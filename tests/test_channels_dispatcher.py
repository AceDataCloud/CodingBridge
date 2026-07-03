"""P1: SessionDispatcher — fan external messages into provider turns."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coding_bridge.channels import (
    ChannelTarget,
    IncomingMessage,
    SendResult,
    SessionDispatcher,
)
from coding_bridge.channels.approvals import ApprovalStore
from coding_bridge.channels.dispatcher import BusyError
from coding_bridge.config import Settings
from coding_bridge.protocol import Event, event_payload

# 5 s ceiling gives slow CI runners headroom while keeping local test runs fast.
_POLL_ITERATIONS = 1000
_POLL_INTERVAL_S = 0.005


async def _wait_until(pred, iterations: int = _POLL_ITERATIONS) -> None:
    for _ in range(iterations):
        if pred():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


class _StubProvider:
    """A provider whose ``start()`` emits scripted events and completes."""

    name = "stub"

    def __init__(self, session_id: str, emit, _ask, script: list[dict[str, Any]] | None = None):
        self._session_id = session_id
        self._emit = emit
        self._script = (
            script
            if script is not None
            else [
                event_payload(Event.SESSION_TEXT, session_id, text="hello, "),
                event_payload(Event.SESSION_TEXT, session_id, text="world"),
                event_payload(Event.SESSION_RESULT, session_id, text="hello, world"),
            ]
        )

    async def start(self, _prompt, **_kw):
        for payload in self._script:
            await self._emit(payload)

    async def send(self, _prompt, **_kw):
        return

    async def edit(self, *_a, **_kw):
        return

    async def interrupt(self):
        return

    async def aclose(self):
        return


def _factory(script=None):
    def _make(_provider_name, session_id, emit, ask):
        return _StubProvider(session_id, emit, ask, script=script)

    return _make


class _RecordingAdapter:
    name = "wechat"
    instance_id = "beijing-cvm"

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self._handler = None

    def set_handler(self, h):
        self._handler = h

    async def run(self):
        return

    async def send(self, target: ChannelTarget, text: str, *, reply_to=None) -> SendResult:
        self.replies.append((target.conversation_id, text))
        return SendResult(ok=True, upstream_id="upstream-1", latency_ms=42)

    async def aclose(self):
        return


def _msg(sender: str = "wxid_a", conv: str = "wxid_a", text: str = "/ask hi") -> IncomingMessage:
    return IncomingMessage(
        sender_id=sender,
        sender_name=None,
        target=ChannelTarget(conversation_id=conv),
        text=text,
        msg_type="text",
        direction="inbound",
    )


@pytest.mark.asyncio
async def test_handle_message_concatenates_text_and_sends_reply(tmp_path):
    settings = Settings(config_dir=tmp_path)
    dispatcher = SessionDispatcher(settings, provider_factory=_factory(), default_provider="stub")
    adapter = _RecordingAdapter()

    await dispatcher.handle_message(_msg(text="/ask ping"), adapter)
    await _wait_until(lambda: bool(adapter.replies))

    assert adapter.replies == [("wxid_a", "hello, world")]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_busy_error_when_second_message_for_same_key_arrives(tmp_path):
    """A second message for the same (sender, conversation) while a turn is in flight raises."""

    barrier = asyncio.Event()

    class _SlowProvider(_StubProvider):
        async def start(self, _prompt, **_kw):
            await barrier.wait()
            await self._emit(event_payload(Event.SESSION_RESULT, self._session_id, text="ok"))

    def _slow_factory(_name, session_id, emit, ask):
        return _SlowProvider(session_id, emit, ask)

    settings = Settings(config_dir=tmp_path)
    dispatcher = SessionDispatcher(
        settings, provider_factory=_slow_factory, default_provider="stub"
    )
    adapter = _RecordingAdapter()

    await dispatcher.handle_message(_msg(), adapter)
    with pytest.raises(BusyError):
        await dispatcher.handle_message(_msg(), adapter)
    barrier.set()
    await _wait_until(lambda: bool(adapter.replies))
    assert adapter.replies == [("wxid_a", "ok")]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_different_conversations_run_concurrently(tmp_path):
    """Two messages from different conversations get independent turns."""

    settings = Settings(config_dir=tmp_path)
    dispatcher = SessionDispatcher(settings, provider_factory=_factory(), default_provider="stub")
    adapter = _RecordingAdapter()

    await dispatcher.handle_message(_msg(sender="a", conv="a", text="/ask 1"), adapter)
    await dispatcher.handle_message(_msg(sender="b", conv="b", text="/ask 2"), adapter)
    await _wait_until(lambda: len(adapter.replies) >= 2)

    convs = sorted(c for c, _ in adapter.replies)
    assert convs == ["a", "b"]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_reply_sink_override_receives_final_text(tmp_path):
    """A caller-supplied reply_sink is used in preference to adapter.send()."""

    settings = Settings(config_dir=tmp_path)
    captured: list[tuple[str, str]] = []

    async def _sink(adapter, target, text):
        captured.append((adapter.name, text))
        return SendResult(ok=True)

    dispatcher = SessionDispatcher(
        settings,
        provider_factory=_factory(),
        reply_sink=_sink,
        default_provider="stub",
    )
    adapter = _RecordingAdapter()
    await dispatcher.handle_message(_msg(text="/ask hi"), adapter)
    await _wait_until(lambda: bool(captured))

    assert captured == [("wechat", "hello, world")]
    assert adapter.replies == []  # sink bypassed .send()
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_falls_back_to_no_reply_when_provider_emits_nothing(tmp_path):
    """Provider that never emits any event → dispatcher timeout path fires."""

    empty_script: list[dict[str, Any]] = []

    async def _sink(_adapter, _target, text):
        _sink.captured = text  # type: ignore[attr-defined]
        return SendResult(ok=True)

    _sink.captured = None  # type: ignore[attr-defined]

    settings = Settings(config_dir=tmp_path)
    dispatcher = SessionDispatcher(
        settings,
        provider_factory=_factory(script=empty_script),
        reply_sink=_sink,
        turn_timeout=0.1,
        default_provider="stub",
    )
    adapter = _RecordingAdapter()

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: _sink.captured is not None)  # type: ignore[attr-defined]

    assert "timed out" in (_sink.captured or "")  # type: ignore[attr-defined]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_provider_error_takes_precedence_over_result_text(tmp_path):
    """A buggy provider emitting SESSION_RESULT then SESSION_ERROR must surface the error."""

    def _factory_err(_name, session_id, emit, _ask):
        async def _start(_prompt, **_kw):
            await emit(event_payload(Event.SESSION_RESULT, session_id, text="stale"))
            await emit(event_payload(Event.SESSION_ERROR, session_id, message="boom"))

        prov = _StubProvider(session_id, emit, _ask, script=[])
        prov.start = _start  # type: ignore[method-assign]
        return prov

    settings = Settings(config_dir=tmp_path)
    dispatcher = SessionDispatcher(settings, provider_factory=_factory_err, default_provider="stub")
    adapter = _RecordingAdapter()

    await dispatcher.handle_message(_msg(), adapter)
    await _wait_until(lambda: bool(adapter.replies))

    assert adapter.replies == [("wxid_a", "(provider error: boom)")]
    await dispatcher.aclose()


# --------------------------------------------------------------------------- #
# Live tool approvals (opt-in require_approval)
# --------------------------------------------------------------------------- #


class _ApprovalProvider:
    """Asks for one tool permission, then reports the verdict as its reply."""

    name = "stub"

    def __init__(self, session_id, emit, ask):
        self._session_id = session_id
        self._emit = emit
        self._ask = ask

    async def start(self, _prompt, **_kw):
        resolution = await self._ask(
            "Bash", {"command": "git status"}, {"title": "Run git status"}
        )
        await self._emit(
            event_payload(
                Event.SESSION_RESULT, self._session_id, text=f"decision={resolution.decision}"
            )
        )

    async def send(self, *_a, **_kw):
        return

    async def edit(self, *_a, **_kw):
        return

    async def interrupt(self):
        return

    async def aclose(self):
        return


def _approval_factory():
    def _make(_name, session_id, emit, ask):
        return _ApprovalProvider(session_id, emit, ask)

    return _make


@pytest.mark.asyncio
async def test_require_approval_publishes_and_resolves_on_allow(tmp_path):
    settings = Settings(config_dir=tmp_path)
    store = ApprovalStore(tmp_path / "approvals", ttl=60.0)
    dispatcher = SessionDispatcher(
        settings,
        provider_factory=_approval_factory(),
        default_provider="stub",
        turn_timeout=8.0,
        approval_store=store,
        require_approval=True,
    )
    adapter = _RecordingAdapter()
    await dispatcher.handle_message(_msg(text="/ask do it"), adapter)

    await _wait_until(lambda: len(store.list_pending()) == 1)
    pending = store.list_pending()[0]
    assert pending["tool"] == "Bash"
    assert "git status" in pending["input_preview"]

    assert store.decide(pending["id"], "allow")
    await _wait_until(lambda: bool(adapter.replies))
    assert adapter.replies[-1][1] == "decision=allow"
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_require_approval_denies_on_portal_deny(tmp_path):
    settings = Settings(config_dir=tmp_path)
    store = ApprovalStore(tmp_path / "approvals", ttl=60.0)
    dispatcher = SessionDispatcher(
        settings,
        provider_factory=_approval_factory(),
        default_provider="stub",
        turn_timeout=8.0,
        approval_store=store,
        require_approval=True,
    )
    adapter = _RecordingAdapter()
    await dispatcher.handle_message(_msg(text="/ask do it"), adapter)
    await _wait_until(lambda: len(store.list_pending()) == 1)
    store.decide(store.list_pending()[0]["id"], "deny")
    await _wait_until(lambda: bool(adapter.replies))
    assert adapter.replies[-1][1] == "decision=deny"
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_no_store_use_when_require_approval_disabled(tmp_path):
    settings = Settings(config_dir=tmp_path)
    settings.permission_timeout = 0.4  # broker denies quickly with no approver
    store = ApprovalStore(tmp_path / "approvals", ttl=60.0)
    dispatcher = SessionDispatcher(
        settings,
        provider_factory=_approval_factory(),
        default_provider="stub",
        turn_timeout=8.0,
        approval_store=store,
        require_approval=False,
    )
    adapter = _RecordingAdapter()
    await dispatcher.handle_message(_msg(text="/ask do it"), adapter)
    await _wait_until(lambda: bool(adapter.replies))
    assert store.list_pending() == []
    assert adapter.replies[-1][1] == "decision=deny"
    await dispatcher.aclose()
