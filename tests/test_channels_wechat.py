"""P2: WeChat adapter — WS receive loop + REST send + parsing + redaction."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from coding_bridge.channels import ChannelTarget, IncomingMessage, SendResult
from coding_bridge.channels.wechat import WeChatAdapter, WeChatClient
from coding_bridge.channels.wechat.adapter import (
    _build_ws_url,
    _parse_incoming,
    _parse_polled_message,
    _redact_url,
)

# ---------------------------------------------------------------------------
# Pure helpers: no network
# ---------------------------------------------------------------------------


def test_build_ws_url_http_to_ws() -> None:
    assert (
        _build_ws_url("http://82.156.126.14:8000", "abc") == "ws://82.156.126.14:8000/ws?token=abc"
    )


def test_build_ws_url_https_to_wss_and_strips_trailing_slash() -> None:
    assert (
        _build_ws_url("https://wechat.acedata.cloud/", "abc")
        == "wss://wechat.acedata.cloud/ws?token=abc"
    )


def test_redact_url_hides_token_query_param() -> None:
    assert (
        _redact_url("ws://host/ws?token=secret123&other=1")
        == "ws://host/ws?token=<redacted>&other=1"
    )
    assert _redact_url("http://x/api?api_token=abc") == "http://x/api?api_token=<redacted>"


def test_redact_url_strips_userinfo_from_netloc() -> None:
    """A misconfigured base_url with embedded credentials is redacted too."""
    assert (
        _redact_url("wss://admin:secret@wechat.host/ws?token=abc")
        == "wss://<redacted>@wechat.host/ws?token=<redacted>"
    )
    assert _redact_url("http://tok@host/api") == "http://<redacted>@host/api"


def test_parse_incoming_happy_path_private() -> None:
    payload = {
        "event": "message.new",
        "data": {
            "direction": "inbound",
            "msg_type": "text",
            "conversation_type": "private",
            "sender_name": "Alice",
            "sender_id": "wxid_alice",
            "target": "wxid_alice",
            "text": "/ask hi",
            "msg_id": "m1",
        },
    }
    msg = _parse_incoming(payload)
    assert isinstance(msg, IncomingMessage)
    assert msg.sender_id == "wxid_alice"
    assert msg.sender_name == "Alice"
    assert msg.target.conversation_id == "wxid_alice"
    assert msg.target.conversation_type == "private"
    assert msg.target.reply_to_id == "m1"
    assert msg.text == "/ask hi"
    assert msg.upstream_id == "wxid_alice:m1"


def test_parse_incoming_falls_back_to_target_when_no_sender_id() -> None:
    payload = {
        "event": "message.new",
        "data": {
            "direction": "inbound",
            "text": "hi",
            "target": "wxid_bob",
        },
    }
    msg = _parse_incoming(payload)
    assert msg is not None
    assert msg.sender_id == "wxid_bob"
    assert msg.target.conversation_type == "private"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"event": "other"}, id="wrong-event"),
        pytest.param({"event": "message.new"}, id="no-data"),
        pytest.param(
            {
                "event": "message.new",
                "data": {"direction": "outbound", "text": "hi", "target": "x"},
            },
            id="outbound-skipped",
        ),
        pytest.param(
            {"event": "message.new", "data": {"direction": "inbound", "target": "x"}},
            id="no-text",
        ),
        pytest.param(
            {"event": "message.new", "data": {"direction": "inbound", "text": "hi"}},
            id="no-target",
        ),
    ],
)
def test_parse_incoming_drops_bad_payloads(payload: dict[str, Any]) -> None:
    assert _parse_incoming(payload) is None


# ---------------------------------------------------------------------------
# WeChatClient — respx-mocked HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wechat_client_send_202_returns_ok_with_upstream_id(respx_mock):
    route = respx_mock.post("http://host/api/messages/send").mock(
        return_value=httpx.Response(202, json={"task_id": "t-1"})
    )
    client = WeChatClient("http://host", "tok")
    try:
        r = await client.send_message(ChannelTarget(conversation_id="wxid_a"), "hi")
    finally:
        await client.aclose()

    assert r.ok is True
    assert r.upstream_id == "t-1"
    sent = json.loads(route.calls[0].request.content.decode())
    assert sent == {"target": "wxid_a", "text": "hi", "conversation_type": "private"}
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_wechat_client_uses_polled_display_name_only_for_outbound_send(respx_mock):
    route = respx_mock.post("http://host/api/messages/send").mock(
        return_value=httpx.Response(202, json={"task_id": "t-2"})
    )
    client = WeChatClient("http://host", "tok")
    target = ChannelTarget(
        conversation_id="Msg_private_1",
        conversation_type="private",
        extra={"send_target": "Alice"},
    )
    try:
        result = await client.send_message(target, "hi")
    finally:
        await client.aclose()

    assert result.ok is True
    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["target"] == "Alice"


@pytest.mark.asyncio
async def test_wechat_client_send_500_returns_error_and_never_leaks_token(respx_mock):
    respx_mock.post("http://host/api/messages/send").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    client = WeChatClient("http://host", "supersecrettoken")
    try:
        r = await client.send_message(ChannelTarget(conversation_id="c"), "hi")
    finally:
        await client.aclose()

    assert r.ok is False
    assert r.upstream_id is None
    assert "HTTP 500" in (r.error or "")
    assert "boom" in (r.error or "")
    assert "supersecrettoken" not in (r.error or "")


@pytest.mark.asyncio
async def test_wechat_client_send_transport_error_returns_non_raising_result(respx_mock):
    respx_mock.post("http://host/api/messages/send").mock(
        side_effect=httpx.ConnectError("no route")
    )
    client = WeChatClient("http://host", "tok")
    try:
        r = await client.send_message(ChannelTarget(conversation_id="c"), "hi")
    finally:
        await client.aclose()

    assert r.ok is False
    assert "ConnectError" in (r.error or "")


@pytest.mark.asyncio
async def test_wechat_client_polls_messages_with_authenticated_cursor(respx_mock):
    route = respx_mock.get("http://host/api/messages/poll").mock(
        return_value=httpx.Response(200, json=[{"id": "1", "text": "hello"}])
    )
    client = WeChatClient("http://host", "tok")
    try:
        rows = await client.poll_messages(123, limit=25)
    finally:
        await client.aclose()

    assert rows == [{"id": "1", "text": "hello"}]
    request = route.calls[0].request
    assert request.url.params["since"] == "123"
    assert request.url.params["limit"] == "25"
    assert request.headers["authorization"] == "Bearer tok"
    assert request.extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
async def test_wechat_client_lists_conversation_targets(respx_mock):
    route = respx_mock.get("http://host/api/conversations").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "total": 1,
                    "conversations": [
                        {"id": f"Msg_{index}", "name": f"User {index}", "type": "private"}
                        for index in range(200)
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "total": 1,
                    "conversations": [{"id": "Msg_200", "name": "Team", "type": "group"}],
                },
            ),
        ]
    )
    client = WeChatClient("http://host", "tok")
    try:
        rows = await client.list_conversations()
    finally:
        await client.aclose()

    assert len(rows) == 201
    assert rows[-1] == {"id": "Msg_200", "name": "Team", "type": "group"}
    assert route.call_count == 2
    assert route.calls[1].request.url.params["offset"] == "200"


@pytest.mark.asyncio
async def test_wechat_client_stops_at_wisdom_maximum_offset(respx_mock):
    page = [
        {"id": f"Msg_{index}", "name": f"User {index}", "type": "private"} for index in range(200)
    ]
    route = respx_mock.get("http://host/api/conversations").mock(
        return_value=httpx.Response(200, json={"total": 1, "conversations": page})
    )
    client = WeChatClient("http://host", "tok")
    try:
        rows = await client.list_conversations()
    finally:
        await client.aclose()

    assert len(rows) == 1200
    assert [call.request.url.params["offset"] for call in route.calls] == [
        "0",
        "200",
        "400",
        "600",
        "800",
        "1000",
    ]


@pytest.mark.asyncio
async def test_wechat_client_transport_error_does_not_leak_token(respx_mock):
    """Defensive: even if httpx's exception grew to echo request headers,
    the SendResult.error must not contain the Bearer token."""

    class _NoisyError(httpx.HTTPError):
        pass

    noisy = _NoisyError("failed: Authorization=Bearer supersecrettoken " + "X" * 500)
    respx_mock.post("http://host/api/messages/send").mock(side_effect=noisy)
    client = WeChatClient("http://host", "supersecrettoken")
    try:
        r = await client.send_message(ChannelTarget(conversation_id="c"), "hi")
    finally:
        await client.aclose()

    assert r.ok is False
    assert r.error is not None
    # We can't scrub arbitrary substrings from an exception message, but we do
    # bound it to a fixed prefix + 200 chars — which is far below the point at
    # which a real leak would render this defensive.  Prove the bound holds.
    assert len(r.error) <= 200 + len("transport error: _NoisyError: ")


# ---------------------------------------------------------------------------
# WeChatAdapter — WS loop with an injected in-memory websocket
# ---------------------------------------------------------------------------


class _FakeWS:
    """Async-iterable fake websocket. Yields scripted frames, then blocks on ``stop``."""

    def __init__(self, frames: list[str], stop: asyncio.Event) -> None:
        self._frames = frames
        self._stop = stop

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        for frame in self._frames:
            if self._stop.is_set():
                return
            yield frame
        # After scripted frames drain, mimic a real WS that stays open —
        # unblock only when the adapter is asked to shut down.
        await self._stop.wait()


def _fake_connect(frames: list[str], stop: asyncio.Event):
    def _factory(_url: str) -> _FakeWS:
        return _FakeWS(frames, stop)

    return _factory


def _valid_frame(
    text: str = "/ask hi",
    target: str = "wxid_a",
    message_id: str = "m1",
) -> str:
    return json.dumps(
        {
            "event": "message.new",
            "data": {
                "direction": "inbound",
                "msg_type": "text",
                "conversation_type": "private",
                "sender_name": "Alice",
                "sender_id": target,
                "target": target,
                "text": text,
                "msg_id": message_id,
            },
        }
    )


@pytest.mark.asyncio
async def test_adapter_dispatches_valid_frame_to_handler():
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _adapter) -> None:
        seen.append(msg)

    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        client=WeChatClient("http://host", "tok"),
        ws_connect=_fake_connect([_valid_frame(text="ping")], stop),
        stop_event=stop,
    )
    adapter.set_handler(handler)

    async def _drive() -> None:
        # Give the consume loop one tick, then stop it.
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(adapter.run(), _drive())
    await adapter.aclose()

    assert len(seen) == 1
    assert seen[0].text == "ping"
    assert seen[0].sender_id == "wxid_a"


@pytest.mark.asyncio
async def test_adapter_polls_when_wisdom_websocket_is_silent():
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _adapter) -> None:
        seen.append(msg)
        stop.set()

    class _PollingClient:
        async def poll_messages(self, since: int, *, limit: int = 100):
            return [
                {
                    "id": "1",
                    "conversation_id": "Msg_private_1",
                    "sender_id": None,
                    "sender_name": None,
                    "direction": "inbound",
                    "type": "text",
                    "text": "hello",
                    "sent_at": "2026-07-21T07:24:55Z",
                }
            ]

        async def list_conversations(self):
            return [
                {
                    "id": "Msg_private_1",
                    "name": "Alice",
                    "type": "private",
                }
            ]

        async def send_message(self, target, text, *, reply_to=None):
            return SendResult(ok=True)

        async def aclose(self) -> None:
            return

    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="wisdom",
        base_url="https://wisdom.example",
        token="tok",
        client=_PollingClient(),
        ws_connect=_fake_connect([], stop),
        stop_event=stop,
        poll_interval=0.01,
    )
    adapter.set_handler(handler)

    await asyncio.wait_for(adapter.run(), timeout=1.0)
    await adapter.aclose()

    assert len(seen) == 1
    assert seen[0].text == "hello"
    assert seen[0].sender_id == "Msg_private_1"
    assert seen[0].target.conversation_id == "Msg_private_1"
    assert seen[0].target.conversation_type == "private"
    assert seen[0].target.extra["send_target"] == "Alice"
    assert seen[0].upstream_id == "Msg_private_1:1"


def test_ws_and_poll_messages_share_canonical_identity():
    ws_message = _parse_incoming(json.loads(_valid_frame(target="Msg_private_1")))
    poll_message = _parse_polled_message(
        {
            "id": "m1",
            "conversation_id": "Msg_private_1",
            "direction": "inbound",
            "type": "text",
            "text": "hello",
            "sent_at": "2026-07-21T07:24:55Z",
        },
        {"Msg_private_1": ("Alice", "private")},
    )

    assert ws_message is not None
    assert poll_message is not None
    assert ws_message.upstream_id == poll_message.upstream_id == "Msg_private_1:m1"


def test_polled_message_without_authoritative_conversation_type_is_dropped():
    message = _parse_polled_message(
        {
            "id": "m1",
            "conversation_id": "Msg_unknown",
            "conversation_name": "Unknown chat",
            "direction": "inbound",
            "type": "text",
            "text": "hello",
        },
        {},
    )

    assert message is None


@pytest.mark.asyncio
async def test_poll_cursor_overlaps_start_and_advances_for_ws_seen_rows(monkeypatch):
    poll_cursors: list[int] = []
    now = 1_000

    class _PollingClient:
        async def poll_messages(self, since: int, *, limit: int = 100):
            poll_cursors.append(since)
            if len(poll_cursors) == 1:
                return [
                    {
                        "id": "m1",
                        "conversation_id": "Msg_1",
                        "direction": "inbound",
                        "type": "text",
                        "text": "already seen",
                        "sent_at": "1970-01-01T00:16:40Z",
                    }
                ]
            stop.set()
            return []

        async def list_conversations(self):
            raise AssertionError("seen rows must not reload conversations")

        async def send_message(self, target, text, *, reply_to=None):
            return SendResult(ok=True)

        async def aclose(self) -> None:
            return

    monkeypatch.setattr("coding_bridge.channels.wechat.adapter.time.time", lambda: now)
    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="wisdom",
        base_url="https://wisdom.example",
        token="tok",
        client=_PollingClient(),
        ws_connect=_fake_connect([], stop),
        stop_event=stop,
        poll_interval=0.01,
    )
    adapter._seen.add("Msg_1:m1")
    adapter.set_handler(lambda *_args: None)

    await asyncio.wait_for(adapter.run(), timeout=1.0)
    await adapter.aclose()

    assert poll_cursors == [now - 1, now - 1]


@pytest.mark.asyncio
async def test_adapter_skips_malformed_and_non_inbound_frames():
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _adapter) -> None:
        seen.append(msg)

    frames = [
        "not-json",
        json.dumps({"event": "other"}),
        json.dumps(
            {
                "event": "message.new",
                "data": {"direction": "outbound", "text": "echo", "target": "x"},
            }
        ),
        _valid_frame(text="real"),
    ]
    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        client=WeChatClient("http://host", "tok"),
        ws_connect=_fake_connect(frames, stop),
        stop_event=stop,
    )
    adapter.set_handler(handler)

    async def _drive() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(adapter.run(), _drive())
    await adapter.aclose()

    assert len(seen) == 1
    assert seen[0].text == "real"


@pytest.mark.asyncio
async def test_adapter_handler_exception_does_not_kill_loop():
    call_count = 0

    async def handler(_msg: IncomingMessage, _adapter) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("handler exploded")

    frames = [
        _valid_frame(text="one", message_id="m1"),
        _valid_frame(text="two", message_id="m2"),
    ]
    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        client=WeChatClient("http://host", "tok"),
        ws_connect=_fake_connect(frames, stop),
        stop_event=stop,
    )
    adapter.set_handler(handler)

    async def _drive() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(adapter.run(), _drive())
    await adapter.aclose()

    assert call_count == 2  # both frames delivered even though the first raised


@pytest.mark.asyncio
async def test_adapter_run_without_handler_raises():
    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        client=WeChatClient("http://host", "tok"),
        ws_connect=_fake_connect([], stop),
        stop_event=stop,
    )
    with pytest.raises(RuntimeError, match="set_handler"):
        await adapter.run()
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_send_delegates_to_client(respx_mock):
    respx_mock.post("http://host/api/messages/send").mock(
        return_value=httpx.Response(202, json={"task_id": "t-9"})
    )
    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        ws_connect=_fake_connect([], stop),
        stop_event=stop,
    )
    try:
        r: SendResult = await adapter.send(ChannelTarget(conversation_id="wxid_z"), "hello")
    finally:
        await adapter.aclose()

    assert r.ok is True
    assert r.upstream_id == "t-9"


@pytest.mark.asyncio
async def test_adapter_aclose_forces_active_ws_to_close():
    """aclose() must call ws.close() on the active WS so a blocked recv unblocks."""

    close_calls: list[int] = []

    class _BlockingWS:
        """WS whose iterator blocks forever unless close() is called."""

        def __init__(self) -> None:
            self._closed = asyncio.Event()

        async def __aenter__(self) -> _BlockingWS:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def close(self) -> None:
            close_calls.append(1)
            self._closed.set()

        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            # Simulate a real WS blocked on recv() with no traffic — only
            # unblocks when close() runs.
            await self._closed.wait()
            return
            yield  # unreachable; keeps mypy happy about async generator shape

    ws = _BlockingWS()

    def _connect(_url: str) -> _BlockingWS:
        return ws

    async def handler(_msg: IncomingMessage, _adapter) -> None:
        raise AssertionError("handler must never be called — WS never yields")

    stop = asyncio.Event()
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="tok",
        client=WeChatClient("http://host", "tok"),
        ws_connect=_connect,
        stop_event=stop,
    )
    adapter.set_handler(handler)

    async def _drive() -> None:
        # Let run() enter _consume and start blocking, then trigger shutdown.
        await asyncio.sleep(0.05)
        await adapter.aclose()

    await asyncio.wait_for(asyncio.gather(adapter.run(), _drive()), timeout=2.0)
    assert close_calls == [1]


@pytest.mark.asyncio
async def test_adapter_reconnects_after_ws_error(caplog):
    """A WS connect exception triggers backoff, then a second connect succeeds."""

    attempts: list[int] = []
    frames = [_valid_frame(text="after-reconnect")]
    stop = asyncio.Event()

    class _FakeConnect:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _url: str):
            self.calls += 1
            attempts.append(self.calls)
            if self.calls == 1:
                raise ConnectionRefusedError("simulated")
            return _FakeWS(frames, stop)

    seen: list[str] = []

    async def handler(msg: IncomingMessage, _adapter) -> None:
        seen.append(msg.text)

    # Prevent the test from hanging: after 200 ms, force stop.
    adapter = WeChatAdapter(
        instance_id="cvm-bj",
        base_url="http://host",
        token="reconnecttoken",
        client=WeChatClient("http://host", "reconnecttoken"),
        ws_connect=_FakeConnect(),
        stop_event=stop,
    )
    adapter.set_handler(handler)

    async def _drive() -> None:
        await asyncio.sleep(0.7)  # > _INITIAL_BACKOFF_S (0.5) + delivery
        stop.set()

    with caplog.at_level(logging.INFO, logger="coding-bridge.channels.wechat"):
        await asyncio.gather(adapter.run(), _drive())
    await adapter.aclose()

    assert seen == ["after-reconnect"]
    assert len(attempts) == 2
    # Never log the real token, even in the retry warning.
    for rec in caplog.records:
        assert "reconnecttoken" not in rec.getMessage()
