"""P5: end-to-end smoke — full inbound-message -> reply loop, offline.

This is the integration counterpart to the unit suites: instead of testing
``PolicyGate`` and ``SessionDispatcher`` in isolation, it wires the **real**
``PolicyGate -> SessionDispatcher`` composition (exactly what
``channels_cli.cmd_channels_start`` builds) behind a fake in-memory adapter and
a scripted stub provider, then drives a message through and asserts the reply
comes back out the adapter's ``send()``.

No sockets, no ``claude`` binary, no gateway — every dependency is a stub, so
this runs in CI on every push and guards the wiring that P1/P4/P7 assembled.
The opt-in *real* provider smoke lives behind ``channels smoke`` in the CLI.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coding_bridge.channels import (
    ChannelPolicy,
    ChannelTarget,
    IncomingMessage,
    PolicyGate,
    SendResult,
    SessionDispatcher,
)
from coding_bridge.config import Settings
from coding_bridge.protocol import Event, event_payload

_POLL_ITERATIONS = 1000
_POLL_INTERVAL_S = 0.005


async def _wait_until(pred, iterations: int = _POLL_ITERATIONS) -> None:
    for _ in range(iterations):
        if pred():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


class _ScriptedProvider:
    """Emits a scripted event stream on ``start`` then completes."""

    name = "stub"

    def __init__(self, session_id: str, emit, _ask, script: list[dict[str, Any]]):
        self._session_id = session_id
        self._emit = emit
        self._script = script

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


def _factory_echoing_prompt():
    """Provider factory whose reply echoes back the prompt it received.

    Proves the *prompt actually reaches the provider* (not just that some
    canned text flows back) — the prompt is captured on ``start`` and replayed
    as the session result.
    """

    def _make(_provider_name, session_id, emit, ask):
        captured: dict[str, str] = {}

        class _Echo(_ScriptedProvider):
            async def start(self, prompt, **_kw):
                captured["prompt"] = prompt
                await self._emit(
                    event_payload(Event.SESSION_TEXT, session_id, text=f"echo: {prompt}")
                )
                await self._emit(event_payload(Event.SESSION_RESULT, session_id, text=""))

        return _Echo(session_id, emit, ask, script=[])

    return _make


class _InMemoryAdapter:
    """A channel adapter that records replies instead of hitting a network."""

    name = "wechat"
    instance_id = "smoke-instance"

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self._handler = None

    def set_handler(self, h):
        self._handler = h

    async def run(self):
        return

    async def send(self, target: ChannelTarget, text: str, *, reply_to=None) -> SendResult:
        self.replies.append((target.conversation_id, text))
        return SendResult(ok=True, upstream_id="up-1", latency_ms=1)

    async def aclose(self):
        return

    async def deliver(self, msg: IncomingMessage) -> None:
        """Simulate an inbound message arriving off the wire."""
        assert self._handler is not None, "handler not wired"
        await self._handler(msg, self)


def _msg(text: str, *, sender: str = "wxid_owner") -> IncomingMessage:
    return IncomingMessage(
        sender_id=sender,
        sender_name="Owner",
        target=ChannelTarget(conversation_id=sender),
        text=text,
        msg_type="text",
        direction="inbound",
    )


def _wire(settings: Settings, factory, policy: ChannelPolicy):
    """Build the same PolicyGate -> SessionDispatcher -> adapter graph the CLI does."""
    dispatcher = SessionDispatcher(settings, provider_factory=factory, default_provider="stub")
    gate = PolicyGate(policy, dispatcher.handle_message)
    adapter = _InMemoryAdapter()
    adapter.set_handler(gate.handle)
    return dispatcher, adapter


@pytest.mark.asyncio
async def test_e2e_trigger_message_round_trips_to_reply(tmp_path):
    settings = Settings(config_dir=tmp_path)
    policy = ChannelPolicy(trigger_prefix="/ask ", allowed_senders=("wxid_owner",))
    dispatcher, adapter = _wire(settings, _factory_echoing_prompt(), policy)

    await adapter.deliver(_msg("/ask hello world"))
    await _wait_until(lambda: bool(adapter.replies))

    # Prefix stripped before reaching the provider, and the provider's echo
    # made it all the way back out through the dispatcher + adapter.
    assert adapter.replies == [("wxid_owner", "echo: hello world")]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_e2e_missing_prefix_is_dropped_silently(tmp_path):
    settings = Settings(config_dir=tmp_path)
    policy = ChannelPolicy(trigger_prefix="/ask ", allowed_senders=())
    dispatcher, adapter = _wire(settings, _factory_echoing_prompt(), policy)

    await adapter.deliver(_msg("just chatting, no trigger"))
    # Give the loop a chance to (not) produce a reply.
    await asyncio.sleep(0.05)

    assert adapter.replies == []
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_e2e_sender_not_allowed_is_dropped(tmp_path):
    settings = Settings(config_dir=tmp_path)
    policy = ChannelPolicy(trigger_prefix="/ask ", allowed_senders=("wxid_owner",))
    dispatcher, adapter = _wire(settings, _factory_echoing_prompt(), policy)

    await adapter.deliver(_msg("/ask hi", sender="wxid_stranger"))
    await asyncio.sleep(0.05)

    assert adapter.replies == []
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_e2e_provider_error_surfaces_as_reply(tmp_path):
    settings = Settings(config_dir=tmp_path)
    policy = ChannelPolicy(trigger_prefix="/ask ", allowed_senders=())

    def _err_factory():
        def _make(_pn, session_id, emit, _ask):
            script = [event_payload(Event.SESSION_ERROR, session_id, message="boom")]
            return _ScriptedProvider(session_id, emit, _ask, script=script)

        return _make

    dispatcher, adapter = _wire(settings, _err_factory(), policy)

    await adapter.deliver(_msg("/ask trigger error"))
    await _wait_until(lambda: bool(adapter.replies))

    assert adapter.replies == [("wxid_owner", "(provider error: boom)")]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_e2e_duplicate_message_id_only_replies_once(tmp_path):
    settings = Settings(config_dir=tmp_path)
    policy = ChannelPolicy(trigger_prefix="/ask ", allowed_senders=())
    dispatcher, adapter = _wire(settings, _factory_echoing_prompt(), policy)

    dup = IncomingMessage(
        sender_id="wxid_owner",
        sender_name=None,
        target=ChannelTarget(conversation_id="wxid_owner"),
        text="/ask once",
        msg_type="text",
        direction="inbound",
        upstream_id="same-id-123",
    )
    await adapter.deliver(dup)
    await _wait_until(lambda: bool(adapter.replies))
    # Second delivery with the same upstream_id must be deduped by the gate.
    await adapter.deliver(dup)
    await asyncio.sleep(0.05)

    assert adapter.replies == [("wxid_owner", "echo: once")]
    await dispatcher.aclose()
