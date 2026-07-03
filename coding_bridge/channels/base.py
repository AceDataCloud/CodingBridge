"""Channel-adapter Protocol and value objects shared by every messenger.

An adapter is the thin glue between one external messaging system (WeChat,
later Telegram / Discord …) and the dispatcher. It never spawns provider
processes, tracks sessions, or evaluates prompts — those live in
``SessionDispatcher`` and reuse the existing ``Session`` / ``Provider`` machinery
so the browser and WeChat paths share one coding-agent implementation.

Design invariants:

* ``instance_id`` is opaque to the dispatcher but MUST be stable per adapter
  instance (per configured WeChat gateway endpoint, per Bot token, …) so multi-instance
  deployments can namespace dedup keys, log fields, and outbox rows.
* ``send()`` is expected to return a ``SendResult`` — never raise on a delivery
  failure — because delivery is best-effort and the dispatcher records the
  outcome for observability + retry decisions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ChannelTarget:
    """Where a reply for an incoming message should be delivered.

    ``conversation_id`` is the ID of the "room" — a WeChat private chat wxid, a
    group room id, a Telegram chat_id, etc. ``reply_to_id`` is set only when the
    external protocol supports a reply-to primitive worth carrying (the WeChat
    gateway's ``msg_id``, Telegram's ``reply_to_message_id``); adapters that lack
    one leave it ``None``.
    """

    conversation_id: str
    conversation_type: str = "private"
    reply_to_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncomingMessage:
    """A single external message routed to the dispatcher.

    ``sender_id`` MUST be stable per remote identity (WeChat wxid, Telegram
    user_id, …) — the dispatcher uses it for allowlists, rate limits, and per-
    sender session keying.
    """

    sender_id: str
    sender_name: str | None
    target: ChannelTarget
    text: str
    msg_type: str
    direction: str  # ``inbound`` / ``outbound`` (per the gateway's schema)
    upstream_id: str | None = None
    received_at_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    """Outcome of an adapter ``send()`` call. Non-raising by contract."""

    ok: bool
    upstream_id: str | None = None
    error: str | None = None
    latency_ms: int | None = None


# ``handler(msg, adapter)`` — the dispatcher installs one before ``run()``.
MessageHandler = Callable[["IncomingMessage", "ChannelAdapter"], Awaitable[None]]


@runtime_checkable
class ChannelAdapter(Protocol):
    """One long-lived connection to an external messaging system.

    Lifecycle:

    1. Construct — parse settings, do NOT open sockets yet.
    2. ``set_handler(h)`` — dispatcher registers the callback.
    3. ``run()`` — subscribe loop; must call ``handler(msg, self)`` per inbound
       message. Should reconnect internally on transient failures. Returns only
       on ``aclose()`` or an unrecoverable auth failure.
    4. ``send(target, text)`` — outbound reply.
    5. ``aclose()`` — signal ``run()`` to stop; idempotent.

    Adapters MUST NOT retain references to the dispatcher itself — only the
    handler callback — so the dispatcher can outlive individual adapters (and
    vice versa) without leaks.
    """

    name: str
    instance_id: str

    def set_handler(self, handler: MessageHandler) -> None: ...

    async def run(self) -> None: ...

    async def send(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult: ...

    async def aclose(self) -> None: ...
