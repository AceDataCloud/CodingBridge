"""Per-instance abuse-control policy for channel adapters.

Wraps the ``MessageHandler`` you pass to ``ChannelAdapter.set_handler``. The
adapter has no idea abuse controls exist; the wrapper enforces them **before**
the message ever reaches the ``SessionDispatcher``.

Enforced (in this order, cheapest to most-expensive):

1. **Trigger prefix** — the raw message text must start with a configured
   prefix (default ``"/ask "``) so a bot in a group chat only responds when
   explicitly addressed. Prefix is stripped before the message is forwarded.
   Empty string → prefix check disabled.
2. **Group allowlist** — if ``allowed_groups`` is non-empty, a message from a
   group chat is dropped unless its conversation id is listed. Only affects
   group chats; 1:1 DMs always pass. Empty list → every group allowed.
3. **Sender allowlist** — if ``allowed_senders`` is non-empty, only those
   ``sender_id`` values are accepted. Empty list → allow all (rely on
   ``token`` for auth; useful when only the account owner can reach the
   WeChat gateway endpoint anyway).
4. **Per-sender rate limit** — sliding-window: at most ``rate_limit_per_min``
   messages from one ``sender_id`` in the last 60 s. Default 6/min.

Every rejection is logged at INFO with a stable ``reason`` code so operators
can grep. Nothing sensitive (token, message body) ever appears in the log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .base import ChannelAdapter, IncomingMessage, MessageHandler

logger = logging.getLogger("coding-bridge.channels")


@dataclass(frozen=True)
class ChannelPolicy:
    """Per-instance abuse-control knobs. Frozen — build once at start-up."""

    #: Only accept messages whose text starts with this string (default
    #: ``"/ask "``). Empty string disables the prefix check.
    trigger_prefix: str = "/ask "

    #: Sender IDs that may talk to this bot. Empty tuple = allow all.
    allowed_senders: tuple[str, ...] = ()

    #: Group conversation IDs the bot may answer in. Empty tuple = every group
    #: is allowed (still gated by prefix/sender). Only restricts group chats;
    #: private 1:1 DMs are never filtered by this.
    allowed_groups: tuple[str, ...] = ()

    #: Per-sender sliding window: at most this many messages per 60 s.
    rate_limit_per_min: int = 6

    #: Dedup window in seconds. Repeat upstream ``msg_id`` inside this window
    #: is silently dropped (upstream retries, duplicate deliveries). ``0``
    #: disables dedup — useful in tests.
    dedup_window_seconds: float = 300.0

    #: How many recent ``msg_id`` values to remember per adapter instance.
    dedup_max_ids: int = 1024

    #: Cap on how many distinct ``sender_id`` rate-limit windows we keep in
    #: memory. Beyond this the least-recently-active bucket is dropped so an
    #: attacker spamming with random sender IDs can't grow the map without
    #: bound. Real deployments serve at most a few hundred distinct senders.
    rate_limit_max_senders: int = 4096


@dataclass
class _SlidingWindow:
    """Per-sender timestamps for the last minute."""

    window_seconds: float
    limit: int
    stamps: deque[float] = field(default_factory=deque)
    last_touch: float = 0.0

    def allow(self, now: float) -> bool:
        self.last_touch = now
        cutoff = now - self.window_seconds
        while self.stamps and self.stamps[0] < cutoff:
            self.stamps.popleft()
        if len(self.stamps) >= self.limit:
            return False
        self.stamps.append(now)
        return True


class PolicyGate:
    """Wraps a downstream ``MessageHandler`` with the policy checks above.

    Usage::

        gate = PolicyGate(policy, downstream=dispatcher.handle_message)
        adapter.set_handler(gate.handle)
    """

    def __init__(
        self,
        policy: ChannelPolicy,
        downstream: MessageHandler,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._downstream = downstream
        self._clock = clock
        self._lock = asyncio.Lock()
        self._windows: dict[str, _SlidingWindow] = {}
        # dedup: ordered dict of msg_id → timestamp (arrival). We use a deque
        # for insertion order + dict for O(1) membership. Bounded by
        # ``dedup_max_ids``; oldest evicted first.
        self._dedup_order: deque[str] = deque()
        self._dedup_seen: dict[str, float] = {}

    async def handle(self, msg: IncomingMessage, adapter: ChannelAdapter) -> None:
        """Run all policy checks, then delegate. Never raises."""
        # (1) trigger prefix
        prefix = self._policy.trigger_prefix
        text = msg.text
        if prefix:
            if not text.startswith(prefix):
                self._reject(adapter, msg, "trigger_prefix_missing")
                return
            text = text[len(prefix) :].lstrip()
            if not text:
                self._reject(adapter, msg, "empty_after_prefix")
                return

        # (2) group allowlist — only restricts group chats; 1:1 DMs are never
        # filtered here (a DM's conversation_type is not "group").
        groups = self._policy.allowed_groups
        if (
            groups
            and msg.target.conversation_type == "group"
            and msg.target.conversation_id not in groups
        ):
            self._reject(adapter, msg, "group_not_allowed")
            return

        # (3) sender allowlist
        allowed = self._policy.allowed_senders
        if allowed and msg.sender_id not in allowed:
            self._reject(adapter, msg, "sender_not_allowed")
            return

        # (4+5) rate limit + dedup share the lock so a burst can't race
        async with self._lock:
            # dedup
            if self._policy.dedup_window_seconds > 0 and msg.upstream_id:
                now = self._clock()
                self._prune_dedup(now)
                if msg.upstream_id in self._dedup_seen:
                    self._reject(adapter, msg, "duplicate_msg_id")
                    return
                self._remember(msg.upstream_id, now)

            # rate limit
            if self._policy.rate_limit_per_min > 0:
                now = self._clock()
                self._evict_stale_windows(now)
                window = self._windows.get(msg.sender_id)
                if window is None:
                    # Cap-check BEFORE insert so an attacker spamming with
                    # random sender IDs can never grow the map past the cap.
                    if len(self._windows) >= self._policy.rate_limit_max_senders:
                        self._drop_lru_window()
                    window = _SlidingWindow(
                        window_seconds=60.0, limit=self._policy.rate_limit_per_min
                    )
                    self._windows[msg.sender_id] = window
                if not window.allow(now):
                    self._reject(adapter, msg, "rate_limited")
                    return

        # (6) forward to dispatcher — strip prefix out of the payload
        forwarded = replace(msg, text=text)
        try:
            await self._downstream(forwarded, adapter)
        except Exception as exc:  # dispatcher errors must not kill the adapter
            # NOTE: intentionally `logger.error`, NOT `logger.exception`, so
            # the traceback (which may include `repr(exc)` with the original
            # message text embedded) never reaches the log. The exception
            # class name is enough for operators to grep.
            logger.error(
                "downstream handler raised: adapter=%s instance=%s exc=%s",
                adapter.name,
                adapter.instance_id,
                exc.__class__.__name__,
            )

    def _reject(self, adapter: ChannelAdapter, msg: IncomingMessage, reason: str) -> None:
        # NOTE: never log ``msg.text`` (may contain user content or secrets that
        # look like commands). Log the sender + reason only.
        logger.info(
            "channel message rejected: adapter=%s instance=%s sender=%s reason=%s",
            adapter.name,
            adapter.instance_id,
            msg.sender_id,
            reason,
        )

    def _prune_dedup(self, now: float) -> None:
        cutoff = now - self._policy.dedup_window_seconds
        while self._dedup_order and self._dedup_seen.get(self._dedup_order[0], 0) < cutoff:
            oldest = self._dedup_order.popleft()
            self._dedup_seen.pop(oldest, None)

    def _remember(self, msg_id: str, now: float) -> None:
        # Cap: evict oldest until size is under ``dedup_max_ids``.
        while len(self._dedup_order) >= self._policy.dedup_max_ids:
            oldest = self._dedup_order.popleft()
            self._dedup_seen.pop(oldest, None)
        self._dedup_order.append(msg_id)
        self._dedup_seen[msg_id] = now

    def _evict_stale_windows(self, now: float) -> None:
        """Drop rate-limit windows that haven't fired in 2 × window_seconds.

        Called on the hot path (inside ``self._lock``) so it must be cheap.
        Windows are ~120 s stale — well past their rate-limit window — so
        removing them can't affect a genuine sender's counter.
        """
        stale_cutoff = now - 120.0
        stale = [k for k, w in self._windows.items() if w.last_touch < stale_cutoff]
        for k in stale:
            self._windows.pop(k, None)

    def _drop_lru_window(self) -> None:
        """Evict the least-recently-touched sender window.

        Fires only when the map is at its ``rate_limit_max_senders`` cap AND
        no idle-eviction cleared enough room. Cost is O(N) over the map, but
        N is capped and this path fires at most once per unique-sender
        insertion above the cap.
        """
        if not self._windows:
            return
        lru_key = min(self._windows, key=lambda k: self._windows[k].last_touch)
        self._windows.pop(lru_key, None)


__all__ = ["ChannelPolicy", "PolicyGate"]
