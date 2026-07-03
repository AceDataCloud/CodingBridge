"""Telegram channel: update parsing, client (mock transport), adapter poll loop, config."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from coding_bridge.channels import ChannelTarget, IncomingMessage, SendResult
from coding_bridge.channels.config import (
    ConfigError,
    TelegramInstanceConfig,
    load_channels_config,
    parse_channels_config,
)
from coding_bridge.channels.telegram import TelegramAdapter, TelegramClient, TelegramError
from coding_bridge.channels.telegram.adapter import _parse_update

# ---------------------------------------------------------------------------
# _parse_update — pure, no network
# ---------------------------------------------------------------------------


def _update(
    update_id: int = 1,
    *,
    text: str = "/ask hi",
    chat_id: int = 100,
    chat_type: str = "private",
    from_id: int | None = 555,
    username: str | None = "alice",
    message_id: int = 7,
    date: int = 1_700_000_000,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
        "date": date,
    }
    if from_id is not None:
        msg["from"] = {"id": from_id, "username": username}
    return {"update_id": update_id, "message": msg}


def test_parse_update_private_happy() -> None:
    m = _parse_update(_update())
    assert isinstance(m, IncomingMessage)
    assert m.sender_id == "555"
    assert m.sender_name == "alice"
    assert m.target.conversation_id == "100"
    assert m.target.conversation_type == "private"
    assert m.target.reply_to_id == "7"
    assert m.text == "/ask hi"
    assert m.upstream_id == "1"
    assert m.received_at_ms == 1_700_000_000 * 1000


def test_parse_update_group_is_group_type() -> None:
    m = _parse_update(_update(chat_id=-1001, chat_type="supergroup"))
    assert m is not None
    assert m.target.conversation_type == "group"
    assert m.target.conversation_id == "-1001"


def test_parse_update_sender_falls_back_to_chat_id() -> None:
    m = _parse_update(_update(from_id=None))
    assert m is not None
    assert m.sender_id == "100"
    assert m.sender_name is None


def test_parse_update_uses_first_name_when_no_username() -> None:
    upd = _update()
    upd["message"]["from"] = {"id": 9, "first_name": "Bob"}
    m = _parse_update(upd)
    assert m is not None
    assert m.sender_name == "Bob"


@pytest.mark.parametrize(
    "upd",
    [
        pytest.param({"update_id": 1}, id="no-message"),
        pytest.param({"update_id": 1, "message": "x"}, id="message-not-dict"),
        pytest.param(
            {"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}}},
            id="no-text",
        ),
        pytest.param(
            {"update_id": 1, "message": {"text": "", "chat": {"id": 1, "type": "private"}}},
            id="empty-text",
        ),
        pytest.param({"update_id": 1, "message": {"text": "hi"}}, id="no-chat"),
        pytest.param(
            {"update_id": 1, "message": {"text": "hi", "chat": {"id": "x", "type": "private"}}},
            id="chat-id-not-int",
        ),
        pytest.param(
            {
                "update_id": 1,
                "edited_message": {"text": "hi", "chat": {"id": 1, "type": "private"}},
            },
            id="edited-not-message",
        ),
    ],
)
def test_parse_update_drops_bad(upd: dict[str, Any]) -> None:
    assert _parse_update(upd) is None


# ---------------------------------------------------------------------------
# TelegramClient — httpx.MockTransport (token lives in URL path, never asserted)
# ---------------------------------------------------------------------------


def _transport(routes: dict[str, Any]) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        entry = routes[method]
        return entry(request) if callable(entry) else entry

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_client_get_me_returns_result() -> None:
    tr = _transport(
        {"getMe": httpx.Response(200, json={"ok": True, "result": {"id": 42, "username": "mybot"}})}
    )
    c = TelegramClient("SEKRIT", transport=tr)
    try:
        me = await c.get_me()
    finally:
        await c.aclose()
    assert me == {"id": 42, "username": "mybot"}


@pytest.mark.asyncio
async def test_client_send_message_posts_chat_text_and_reply() -> None:
    seen: dict[str, Any] = {}

    def _send(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    tr = _transport({"sendMessage": _send})
    c = TelegramClient("t", transport=tr)
    try:
        r = await c.send_message(ChannelTarget(conversation_id="100", reply_to_id="7"), "hello")
    finally:
        await c.aclose()
    assert r.ok is True
    assert r.upstream_id == "99"
    assert seen["body"] == {"chat_id": "100", "text": "hello", "reply_to_message_id": 7}
    assert seen["path"].endswith("/sendMessage")


@pytest.mark.asyncio
async def test_client_send_truncates_to_4096() -> None:
    seen: dict[str, Any] = {}

    def _send(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tr = _transport({"sendMessage": _send})
    c = TelegramClient("t", transport=tr)
    try:
        await c.send_message(ChannelTarget(conversation_id="1"), "x" * 5000)
    finally:
        await c.aclose()
    assert len(seen["body"]["text"]) == 4096


@pytest.mark.asyncio
async def test_client_get_updates_sends_offset_and_returns_list() -> None:
    seen: dict[str, Any] = {}

    def _upd(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})

    tr = _transport({"getUpdates": _upd})
    c = TelegramClient("t", transport=tr)
    try:
        res = await c.get_updates(5, 30)
    finally:
        await c.aclose()
    assert res == [{"update_id": 5}]
    assert seen["body"]["offset"] == 5
    assert seen["body"]["allowed_updates"] == ["message"]


@pytest.mark.asyncio
async def test_client_ok_false_raises_with_code_and_no_token() -> None:
    tr = _transport(
        {
            "getMe": httpx.Response(
                200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        }
    )
    c = TelegramClient("supersecrettoken", transport=tr)
    try:
        with pytest.raises(TelegramError) as ei:
            await c.get_me()
    finally:
        await c.aclose()
    assert ei.value.error_code == 401
    assert "supersecrettoken" not in str(ei.value)


@pytest.mark.asyncio
async def test_client_429_carries_retry_after() -> None:
    tr = _transport(
        {
            "getUpdates": httpx.Response(
                200,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 3},
                },
            )
        }
    )
    c = TelegramClient("t", transport=tr)
    try:
        with pytest.raises(TelegramError) as ei:
            await c.get_updates(0, 30)
    finally:
        await c.aclose()
    assert ei.value.error_code == 429
    assert ei.value.retry_after == 3.0


@pytest.mark.asyncio
async def test_client_send_transport_error_returns_result_no_token() -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    tr = _transport({"sendMessage": _boom})
    c = TelegramClient("supersecrettoken", transport=tr)
    try:
        r = await c.send_message(ChannelTarget(conversation_id="1"), "hi")
    finally:
        await c.aclose()
    assert r.ok is False
    assert "supersecrettoken" not in (r.error or "")


@pytest.mark.asyncio
async def test_client_delete_webhook_calls_endpoint() -> None:
    seen: dict[str, Any] = {}

    def _dw(_request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        return httpx.Response(200, json={"ok": True, "result": True})

    tr = _transport({"deleteWebhook": _dw})
    c = TelegramClient("t", transport=tr)
    try:
        await c.delete_webhook()
    finally:
        await c.aclose()
    assert seen.get("called") is True


# ---------------------------------------------------------------------------
# TelegramAdapter — poll loop with an injected fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Scripts ``get_updates`` batches, then blocks until stop (mimics long-poll)."""

    def __init__(self, stop: asyncio.Event, script: list[Any]) -> None:
        self._stop = stop
        self._script = list(script)
        self.offsets: list[int] = []
        self.deleted = 0
        self.sent: list[tuple[str, str, str | None]] = []
        self.closed = False

    async def get_updates(self, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await self._stop.wait()
        return []

    async def delete_webhook(self) -> None:
        self.deleted += 1

    async def send_message(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:
        self.sent.append((target.conversation_id, text, reply_to))
        return SendResult(ok=True, upstream_id="1", latency_ms=0)

    async def aclose(self) -> None:
        self.closed = True


def _mk_adapter(stop: asyncio.Event, script: list[Any], **kw: Any) -> TelegramAdapter:
    return TelegramAdapter(
        instance_id="tg1", token="t", client=_FakeClient(stop, script), stop_event=stop, **kw
    )


async def _run_briefly(adapter: TelegramAdapter, stop: asyncio.Event, delay: float = 0.05) -> None:
    async def _drive() -> None:
        await asyncio.sleep(delay)
        stop.set()

    await asyncio.gather(adapter.run(), _drive())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_dispatches_one_update() -> None:
    stop = asyncio.Event()
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _a: object) -> None:
        seen.append(msg)

    adapter = _mk_adapter(stop, [[_update(text="ping")]])
    adapter.set_handler(handler)
    await _run_briefly(adapter, stop)
    assert [m.text for m in seen] == ["ping"]
    assert seen[0].sender_id == "555"


@pytest.mark.asyncio
async def test_adapter_skips_bad_updates() -> None:
    stop = asyncio.Event()
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _a: object) -> None:
        seen.append(msg)

    script = [
        [
            {"update_id": 1, "edited_message": {"text": "x", "chat": {"id": 1, "type": "private"}}},
            {"update_id": 2, "message": {"chat": {"id": 1, "type": "private"}}},
            _update(update_id=3, text="real"),
        ]
    ]
    adapter = _mk_adapter(stop, script)
    adapter.set_handler(handler)
    await _run_briefly(adapter, stop)
    assert [m.text for m in seen] == ["real"]


@pytest.mark.asyncio
async def test_adapter_advances_offset() -> None:
    stop = asyncio.Event()

    async def handler(_m: IncomingMessage, _a: object) -> None:
        return None

    fake = _FakeClient(stop, [[_update(update_id=41)]])
    adapter = TelegramAdapter(instance_id="tg", token="t", client=fake, stop_event=stop)
    adapter.set_handler(handler)
    await _run_briefly(adapter, stop)
    assert 42 in fake.offsets


@pytest.mark.asyncio
async def test_adapter_stops_on_401() -> None:
    stop = asyncio.Event()

    async def handler(_m: IncomingMessage, _a: object) -> None:
        return None

    adapter = _mk_adapter(stop, [TelegramError("unauthorized", error_code=401)])
    adapter.set_handler(handler)
    # Returns on its own — no stop.set() needed. Would hang (→ TimeoutError) if not.
    await asyncio.wait_for(adapter.run(), timeout=1.0)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_409_deletes_webhook_then_continues() -> None:
    stop = asyncio.Event()
    seen: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, _a: object) -> None:
        seen.append(msg)

    fake = _FakeClient(
        stop, [TelegramError("conflict", error_code=409), [_update(text="after")]]
    )
    adapter = TelegramAdapter(instance_id="tg", token="t", client=fake, stop_event=stop)
    adapter.set_handler(handler)
    await _run_briefly(adapter, stop, delay=0.1)
    assert fake.deleted == 1
    assert [m.text for m in seen] == ["after"]


@pytest.mark.asyncio
async def test_adapter_handler_error_does_not_kill_loop() -> None:
    stop = asyncio.Event()
    seen: list[IncomingMessage] = []
    calls = 0

    async def handler(msg: IncomingMessage, _a: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        seen.append(msg)

    adapter = _mk_adapter(
        stop, [[_update(update_id=1, text="one"), _update(update_id=2, text="two")]]
    )
    adapter.set_handler(handler)
    await _run_briefly(adapter, stop)
    assert [m.text for m in seen] == ["two"]


@pytest.mark.asyncio
async def test_adapter_run_without_handler_raises() -> None:
    stop = asyncio.Event()
    adapter = _mk_adapter(stop, [])
    with pytest.raises(RuntimeError):
        await adapter.run()


@pytest.mark.asyncio
async def test_adapter_send_delegates_to_client() -> None:
    stop = asyncio.Event()
    fake = _FakeClient(stop, [])
    adapter = TelegramAdapter(instance_id="tg", token="t", client=fake, stop_event=stop)
    r = await adapter.send(ChannelTarget(conversation_id="100"), "hi", reply_to="7")
    assert r.ok is True
    assert fake.sent == [("100", "hi", "7")]


@pytest.mark.asyncio
async def test_adapter_aclose_does_not_close_injected_client() -> None:
    stop = asyncio.Event()
    fake = _FakeClient(stop, [])
    adapter = TelegramAdapter(instance_id="tg", token="t", client=fake, stop_event=stop)
    await adapter.aclose()
    # Injected client is caller-owned — the adapter must not close it.
    assert fake.closed is False


def test_adapter_rejects_empty_instance_id() -> None:
    with pytest.raises(ValueError):
        TelegramAdapter(instance_id="", token="t")


def test_adapter_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        TelegramAdapter(instance_id="x", token="")


# ---------------------------------------------------------------------------
# Config — [[channels.telegram]] parsing + validation
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "channels.toml"
    p.write_text(body, encoding="utf-8")
    return p


class TestTelegramConfig:
    def test_single_instance_token_env(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "bot1"
token_env = "TG_TOKEN"
enabled = true
""",
        )
        cfg = load_channels_config(p)
        assert len(cfg.telegram) == 1
        inst = cfg.telegram[0]
        assert inst.instance_id == "bot1"
        assert inst.api_base == "https://api.telegram.org"
        assert inst.token_env == "TG_TOKEN"
        assert inst.enabled is True
        assert inst.trigger_prefix == "/ask "
        assert cfg.enabled_telegram == (inst,)

    def test_api_base_override_trailing_slash_stripped(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
api_base = "https://tg.example.com/"
""",
        )
        assert load_channels_config(p).telegram[0].api_base == "https://tg.example.com"

    def test_defaults(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
""",
        )
        inst = load_channels_config(p).telegram[0]
        assert inst.enabled is False
        assert inst.require_approval is False
        assert inst.rate_limit_per_min == 6
        assert inst.dedup_window_seconds == 300.0
        assert inst.allowed_senders == ()
        assert inst.allowed_groups == ()

    def test_to_policy_maps_fields(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
trigger_prefix = "!go "
allowed_senders = ["555", "666"]
allowed_groups = ["-100"]
rate_limit_per_min = 10
dedup_window_seconds = 120.0
""",
        )
        pol = load_channels_config(p).telegram[0].to_policy()
        assert pol.trigger_prefix == "!go "
        assert pol.allowed_senders == ("555", "666")
        assert pol.allowed_groups == ("-100",)
        assert pol.rate_limit_per_min == 10
        assert pol.dedup_window_seconds == 120.0

    def test_resolve_token_from_env(self) -> None:
        inst = TelegramInstanceConfig(instance_id="b", token_env="TG_X")
        assert inst.resolve_token({"TG_X": "abc"}) == "abc"

    def test_resolve_token_missing_env_raises_without_leaking(self) -> None:
        inst = TelegramInstanceConfig(instance_id="b", token_env="TG_X")
        with pytest.raises(ConfigError, match="unset or empty"):
            inst.resolve_token({})

    def test_duplicate_instance_id_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "dup"
token_env = "A"

[[channels.telegram]]
instance_id = "dup"
token_env = "B"
""",
        )
        with pytest.raises(ConfigError, match="duplicate instance_id"):
            load_channels_config(p)

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
webhook_url = "https://x"
""",
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load_channels_config(p)

    def test_token_env_and_file_conflict(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
token_file = "/tmp/x"
""",
        )
        with pytest.raises(ConfigError, match="not both"):
            load_channels_config(p)

    def test_unknown_provider_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
default_provider = "not-real"
""",
        )
        with pytest.raises(ConfigError, match="unknown default_provider"):
            load_channels_config(p)

    def test_bad_api_base_scheme_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
api_base = "ftp://x"
""",
        )
        with pytest.raises(ConfigError, match="api_base must start with"):
            load_channels_config(p)

    def test_api_base_with_userinfo_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
api_base = "https://user:pass@tg.example.com"
""",
        )
        with pytest.raises(ConfigError, match="embedded credentials"):
            load_channels_config(p)

    def test_allowed_senders_must_be_strings(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.telegram]]
instance_id = "b"
token_env = "T"
allowed_senders = [555]
""",
        )
        with pytest.raises(ConfigError, match="allowed_senders"):
            load_channels_config(p)

    def test_telegram_not_array_raises(self) -> None:
        with pytest.raises(ConfigError, match="array of tables"):
            parse_channels_config({"channels": {"telegram": {"instance_id": "x"}}})

    def test_wechat_and_telegram_coexist(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "wx"
base_url = "http://host"
token_env = "WT"
enabled = true

[[channels.telegram]]
instance_id = "tg"
token_env = "TT"
enabled = true
""",
        )
        cfg = load_channels_config(p)
        assert len(cfg.wechat) == 1
        assert len(cfg.telegram) == 1
        assert len(cfg.enabled_wechat) == 1
        assert len(cfg.enabled_telegram) == 1
