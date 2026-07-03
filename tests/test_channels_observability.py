"""P9: structured, privacy-safe per-turn observability events."""

from __future__ import annotations

import asyncio
import logging

import pytest

from coding_bridge.channels import (
    ChannelTarget,
    IncomingMessage,
    SendResult,
    SessionDispatcher,
)
from coding_bridge.channels.observability import TurnEvent, log_turn
from coding_bridge.config import Settings
from coding_bridge.protocol import Event, event_payload

_POLL_ITERATIONS = 1000
_POLL_INTERVAL_S = 0.005


async def _wait_until(pred, iterations: int = _POLL_ITERATIONS) -> None:
    for _ in range(iterations):
        if pred():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


class _StubProvider:
    name = "stub"

    def __init__(self, session_id, emit, _ask, script):
        self._session_id = session_id
        self._emit = emit
        self._script = script

    async def start(self, _prompt, **_kw):
        for payload in self._script:
            await self._emit(payload)

    async def send(self, *_a, **_kw):
        return

    async def edit(self, *_a, **_kw):
        return

    async def interrupt(self):
        return

    async def aclose(self):
        return


def _factory(script):
    def _make(_pn, session_id, emit, ask):
        return _StubProvider(session_id, emit, ask, script)

    return _make


class _Adapter:
    name = "wechat"
    instance_id = "obs-instance"

    def __init__(self, *, ok: bool = True) -> None:
        self.replies: list[str] = []
        self._ok = ok

    def set_handler(self, _h):
        return

    async def run(self):
        return

    async def send(self, target: ChannelTarget, text: str, *, reply_to=None) -> SendResult:
        self.replies.append(text)
        return SendResult(
            ok=self._ok,
            upstream_id="u",
            latency_ms=1,
            error=None if self._ok else "boom",
        )

    async def aclose(self):
        return


def _msg(text: str = "hello there") -> IncomingMessage:
    return IncomingMessage(
        sender_id="wxid_a",
        sender_name=None,
        target=ChannelTarget(conversation_id="wxid_a"),
        text=text,
        msg_type="text",
        direction="inbound",
    )


def _turn_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("channel turn:")]


# ---------- log_turn in isolation --------------------------------------------


def test_log_turn_emits_one_record_with_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    log_turn(
        TurnEvent(
            adapter="wechat",
            instance_id="i1",
            provider="claude",
            outcome="ok",
            latency_ms=42,
            prompt_chars=10,
            reply_chars=5,
            session_id="sess-xyz",
        )
    )
    recs = _turn_records(caplog)
    assert len(recs) == 1
    rec = recs[0]
    # Structured fields ride on the record for the relay/CLS forwarder.
    assert rec.channel_instance == "i1"
    assert rec.channel_provider == "claude"
    assert rec.channel_outcome == "ok"
    assert rec.channel_latency_ms == 42
    assert rec.channel_prompt_chars == 10
    assert rec.channel_reply_chars == 5
    assert rec.session_id == "sess-xyz"


# ---------- through the dispatcher -------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_logs_ok_turn(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    script = [
        event_payload(Event.SESSION_TEXT, "s", text="hi back"),
        event_payload(Event.SESSION_RESULT, "s", text=""),
    ]
    dispatcher = SessionDispatcher(settings, _factory(script), default_provider="claude")
    adapter = _Adapter()

    await dispatcher.handle_message(_msg("hello there"), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    recs = _turn_records(caplog)
    assert len(recs) == 1
    assert recs[0].channel_outcome == "ok"
    assert recs[0].channel_provider == "claude"
    assert recs[0].channel_instance == "obs-instance"
    assert recs[0].channel_prompt_chars == len("hello there")
    assert recs[0].channel_reply_chars == len("hi back")
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatcher_logs_provider_error_outcome(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    script = [event_payload(Event.SESSION_ERROR, "s", message="kaboom")]
    dispatcher = SessionDispatcher(settings, _factory(script), default_provider="codex")
    adapter = _Adapter()

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    rec = _turn_records(caplog)[0]
    assert rec.channel_outcome == "provider_error"
    assert rec.channel_provider == "codex"
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatcher_logs_timeout_outcome(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    # Provider emits nothing and never completes → dispatcher times out.
    dispatcher = SessionDispatcher(
        settings, _factory([]), turn_timeout=0.05, default_provider="claude"
    )
    adapter = _Adapter()

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    rec = _turn_records(caplog)[0]
    assert rec.channel_outcome == "timeout"
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatcher_logs_send_failed_outcome(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    script = [event_payload(Event.SESSION_RESULT, "s", text="reply body")]
    dispatcher = SessionDispatcher(settings, _factory(script), default_provider="claude")
    adapter = _Adapter(ok=False)  # send() returns ok=False

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    rec = _turn_records(caplog)[0]
    assert rec.channel_outcome == "send_failed"
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_turn_event_never_logs_message_or_reply_content(tmp_path, caplog) -> None:
    """The observability event must carry sizes, never the text itself."""
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    secret_prompt = "SENSITIVE-PROMPT-CONTENT-9931"
    secret_reply = "SENSITIVE-REPLY-CONTENT-4472"
    script = [event_payload(Event.SESSION_RESULT, "s", text=secret_reply)]
    dispatcher = SessionDispatcher(settings, _factory(script), default_provider="claude")
    adapter = _Adapter()

    await dispatcher.handle_message(_msg(secret_prompt), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    # The turn event line + its structured fields must not contain either secret.
    rec = _turn_records(caplog)[0]
    assert secret_prompt not in rec.getMessage()
    assert secret_reply not in rec.getMessage()
    for value in vars(rec).values():
        assert secret_prompt != value
        assert secret_reply != value
    # But the sizes must be right.
    assert rec.channel_prompt_chars == len(secret_prompt)
    assert rec.channel_reply_chars == len(secret_reply)
    await dispatcher.aclose()


class _CrashingProvider:
    name = "stub"

    def __init__(self, *_a, **_kw):
        pass

    async def start(self, _prompt, **_kw):
        raise RuntimeError("startup exploded")

    async def send(self, *_a, **_kw):
        return

    async def edit(self, *_a, **_kw):
        return

    async def interrupt(self):
        return

    async def aclose(self):
        return


@pytest.mark.asyncio
async def test_dispatcher_logs_provider_error_when_start_raises(tmp_path, caplog) -> None:
    """A crash inside session.start() is recorded as provider_error, not ok."""
    caplog.set_level(logging.INFO, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)

    def _crashing_factory(_pn, session_id, emit, ask):
        return _CrashingProvider()

    dispatcher = SessionDispatcher(settings, _crashing_factory, default_provider="claude")
    adapter = _Adapter()

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    rec = _turn_records(caplog)[0]
    assert rec.channel_outcome == "provider_error"
    # An error reply is delivered so the user isn't left hanging.
    assert adapter.replies and adapter.replies[0].startswith("(provider error:")
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_crash_traceback_does_not_leak_prompt_into_logs(tmp_path, caplog) -> None:
    """If session.start() raises with the prompt in the message, the CHANNELS
    logs stay clean. (Session's own error handler is a separate, pre-existing
    relay-path concern outside this module — see PR notes.)"""
    caplog.set_level(logging.DEBUG, logger="coding-bridge.channels")
    settings = Settings(config_dir=tmp_path)
    secret = "LEAK-CANARY-PROMPT-55231"

    class _EchoingCrashProvider(_CrashingProvider):
        async def start(self, prompt, **_kw):
            # Simulate a provider that echoes the prompt in its error.
            raise RuntimeError(f"bad input: {prompt}")

    def _factory_echo(_pn, session_id, emit, ask):
        return _EchoingCrashProvider()

    dispatcher = SessionDispatcher(settings, _factory_echo, default_provider="claude")
    adapter = _Adapter()

    await dispatcher.handle_message(_msg(secret), adapter)
    await _wait_until(lambda: bool(_turn_records(caplog)))

    # Nothing THIS module logs (dispatcher error line + turn event) may carry
    # the prompt — we log the exception TYPE only, never str(exc)/traceback.
    # (caplog's handler sits on the root logger, so filter to channels records;
    # Session's own traceback logging is a separate pre-existing concern.)
    channel_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("coding-bridge.channels")
    )
    assert secret not in channel_text
    rec = _turn_records(caplog)[0]
    assert rec.channel_outcome == "provider_error"
    assert rec.channel_prompt_chars == len(secret)
    await dispatcher.aclose()


