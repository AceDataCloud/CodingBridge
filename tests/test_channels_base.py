"""P1: ChannelAdapter Protocol conformance + value-object smoke tests."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from coding_bridge.channels import (
    ChannelAdapter,
    ChannelTarget,
    IncomingMessage,
    SendResult,
)


class _FakeAdapter:
    """Minimal ChannelAdapter — used only to verify runtime_checkable Protocol."""

    name = "fake"
    instance_id = "inst-1"

    def __init__(self) -> None:
        self.sent: list[tuple[ChannelTarget, str, str | None]] = []
        self.closed = False

    def set_handler(self, handler):  # noqa: ANN001 — test double
        self.handler = handler

    async def run(self) -> None:
        return None

    async def send(self, target, text, *, reply_to=None):  # noqa: ANN001
        self.sent.append((target, text, reply_to))
        return SendResult(ok=True, upstream_id="u1", latency_ms=1)

    async def aclose(self) -> None:
        self.closed = True


def test_channel_adapter_protocol_conformance() -> None:
    """A minimal concrete adapter satisfies the runtime-checkable Protocol."""

    adapter = _FakeAdapter()
    assert isinstance(adapter, ChannelAdapter)


def test_channel_target_is_frozen_and_defaults_private() -> None:
    target = ChannelTarget(conversation_id="wxid_abc")
    assert target.conversation_type == "private"
    assert target.reply_to_id is None
    assert target.extra == {}
    with pytest.raises(FrozenInstanceError):
        target.conversation_id = "other"  # type: ignore[misc]


def test_incoming_message_is_frozen() -> None:
    msg = IncomingMessage(
        sender_id="wxid_sender",
        sender_name="Alice",
        target=ChannelTarget(conversation_id="wxid_room"),
        text="/ask hi",
        msg_type="text",
        direction="inbound",
        upstream_id="msg-1",
    )
    assert msg.sender_id == "wxid_sender"
    assert msg.direction == "inbound"
    with pytest.raises(FrozenInstanceError):
        msg.text = "hijacked"  # type: ignore[misc]


def test_send_result_defaults() -> None:
    r = SendResult(ok=False, error="429 too many")
    assert r.ok is False
    assert r.upstream_id is None
    assert r.error == "429 too many"
    assert r.latency_ms is None


def test_send_returns_send_result() -> None:
    """Contract: send() returns a SendResult, never raises for delivery failure."""

    async def _go() -> None:
        adapter = _FakeAdapter()
        r = await adapter.send(ChannelTarget(conversation_id="c1"), "hi")
        assert isinstance(r, SendResult)
        assert r.ok is True

    asyncio.run(_go())
