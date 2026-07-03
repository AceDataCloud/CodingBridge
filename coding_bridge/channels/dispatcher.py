"""Route external messages to per-session provider turns and post replies back.

The dispatcher is the channel-side counterpart of ``BridgeConnection`` — it owns
the map from ``(instance_id, sender_id, conversation_id)`` to a live ``Session``,
converts each session's event stream into a single text reply, and hands the
reply to the originating adapter's ``send()``.

Session lifecycle (v1 = one-shot per message):

* Every inbound message spawns a fresh ``Session`` and immediately closes it
  when the provider emits ``session.result`` or ``session.error``. Persisting
  context across messages ("resume") is deferred so v1 stays simple and each
  send/receive pair is auditable in isolation.

* Concurrency guard: only one in-flight turn per session key. A second message
  arriving before the current turn completes is deferred to the caller (the
  adapter decides whether to buffer, drop, or warn) via a ``BusyError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..protocol import Event
from ..providers.base import ProviderFactory
from ..session import Session
from .base import ChannelAdapter, ChannelTarget, IncomingMessage, SendResult
from .observability import TurnEvent, TurnOutcome, log_turn

logger = logging.getLogger("coding-bridge.channels")

# ``(adapter_name, instance_id, sender_id, conversation_id)`` — the key namespace
# the dispatcher uses to route replies. ``adapter_name`` guards against future
# collisions when a user runs both a WeChat and a Telegram adapter at once.
SessionKey = tuple[str, str, str, str]

# Emitted after a session finishes; carries the concatenated reply text so the
# adapter can post it. Never raises — the dispatcher swallows adapter errors and
# logs them so a broken adapter can't kill sibling sessions.
ReplySink = Callable[[ChannelAdapter, ChannelTarget, str], Awaitable[SendResult]]


class BusyError(RuntimeError):
    """Raised by ``handle_message`` when a session key already has an in-flight turn."""


class _Turn:
    """Accumulates text_delta / text / result events for one provider turn."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._done = asyncio.Event()
        self._error: str | None = None

    def on_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event in (Event.SESSION_TEXT, Event.SESSION_TEXT_DELTA):
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._parts.append(text)
        elif event == Event.SESSION_RESULT:
            # Some providers emit their final text only in ``result.text`` and
            # never stream a session.text — pick that up so we don't reply blank.
            result_text = payload.get("text") or payload.get("result")
            if isinstance(result_text, str) and result_text and not self._parts:
                self._parts.append(result_text)
            self._done.set()
        elif event == Event.SESSION_ERROR:
            err = payload.get("message") or payload.get("code") or "provider_error"
            self._error = str(err)
            self._done.set()
        elif event == Event.SESSION_CLOSED:
            self._done.set()

    def text(self) -> str:
        return "".join(self._parts).strip()

    def error(self) -> str | None:
        return self._error

    async def wait(self, timeout: float | None) -> bool:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True


class SessionDispatcher:
    """Fan external messages into ``Session`` turns and fan replies back out."""

    def __init__(
        self,
        settings: Settings,
        provider_factory: ProviderFactory,
        *,
        reply_sink: ReplySink | None = None,
        turn_timeout: float = 300.0,
        default_provider: str = "claude",
    ) -> None:
        self.settings = settings
        self._factory = provider_factory
        self._reply_sink = reply_sink or _default_reply_sink
        self._turn_timeout = turn_timeout
        self._default_provider = default_provider
        self._inflight: dict[SessionKey, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    def key_for(self, msg: IncomingMessage, adapter: ChannelAdapter) -> SessionKey:
        return (adapter.name, adapter.instance_id, msg.sender_id, msg.target.conversation_id)

    async def handle_message(self, msg: IncomingMessage, adapter: ChannelAdapter) -> None:
        """Spawn one turn for ``msg``. Raises ``BusyError`` if a turn is in flight.

        Non-raising for anything else — a provider crash or send failure is
        logged and reported to the caller only via the returned coroutine
        completing (there is no exception to re-raise on the adapter thread).
        """
        key = self.key_for(msg, adapter)
        async with self._lock:
            if key in self._inflight and not self._inflight[key].done():
                raise BusyError(f"turn already running for {key!r}")
            task = asyncio.create_task(self._run_turn(key, msg, adapter))
            self._inflight[key] = task
        # Fire-and-forget: the adapter loop must not block on this turn.

    async def aclose(self) -> None:
        async with self._lock:
            tasks = [t for t in self._inflight.values() if not t.done()]
            self._inflight.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t

    async def _run_turn(
        self, key: SessionKey, msg: IncomingMessage, adapter: ChannelAdapter
    ) -> None:
        session_id = uuid.uuid4().hex
        turn = _Turn()

        async def emit(payload: dict[str, Any]) -> None:
            turn.on_event(payload)

        session = Session(
            session_id=session_id,
            provider_factory=self._factory,
            emit=emit,
            settings=self.settings,
            cwd=self.settings.default_cwd,
            model=self.settings.default_model,
            permission_mode="default",
            provider=self._default_provider,
        )
        started = time.monotonic()
        outcome: TurnOutcome = "ok"
        reply = ""
        sent = False
        try:
            await session.start(msg.text)
            ok = await turn.wait(self._turn_timeout)
            reply = turn.text()
            # Error takes precedence over any text already streamed — if the
            # provider emits SESSION_RESULT and then SESSION_ERROR (buggy
            # provider), we surface the error instead of silently returning the
            # stale text.
            if turn.error():
                reply = f"(provider error: {turn.error()})"
                outcome = "provider_error"
            elif not ok:
                # Partial text on timeout is kept (better than nothing) but the
                # turn is still recorded as a timeout for observability.
                outcome = "timeout"
                reply = reply or "(provider timed out; no reply)"
            if not reply:
                reply = "(no reply)"
                outcome = "empty"
            try:
                result = await self._reply_sink(adapter, msg.target, reply)
                sent = True
            except Exception as exc:  # defensive: adapter send should not kill dispatcher
                logger.exception("channel send failed: %s", exc)
                outcome = "send_failed"
                return
            if not result.ok:
                outcome = "send_failed"
                logger.warning(
                    "channel send returned failure: adapter=%s instance=%s error=%s",
                    adapter.name,
                    adapter.instance_id,
                    result.error,
                )
        except Exception as exc:  # provider/session crash — never kill the task loop
            outcome = "provider_error"
            # Log only the exception TYPE, never str(exc)/traceback — an exception
            # raised out of session.start(msg.text) may echo the user's prompt, which
            # must not reach the log file / relay forwarder.
            logger.error(
                "turn failed: adapter=%s instance=%s error_type=%s",
                adapter.name,
                adapter.instance_id,
                type(exc).__name__,
            )
            if not sent:  # avoid a second message if the reply already went out
                with contextlib.suppress(Exception):
                    await self._reply_sink(adapter, msg.target, f"(provider error: {exc})")
        finally:
            # One structured, content-free event per turn (see observability.py).
            with contextlib.suppress(Exception):
                log_turn(
                    TurnEvent(
                        adapter=adapter.name,
                        instance_id=adapter.instance_id,
                        provider=self._default_provider,
                        outcome=outcome,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        prompt_chars=len(msg.text or ""),
                        reply_chars=len(reply),
                        session_id=session_id,
                    )
                )
            with contextlib.suppress(BaseException):
                await session.close()
            with contextlib.suppress(BaseException):
                async with self._lock:
                    if self._inflight.get(key) is asyncio.current_task():
                        self._inflight.pop(key, None)


async def _default_reply_sink(
    adapter: ChannelAdapter, target: ChannelTarget, text: str
) -> SendResult:
    """Fallback sink used when the caller didn't supply one — just calls send()."""

    return await adapter.send(target, text)


__all__ = ["SessionDispatcher", "BusyError", "ReplySink"]
