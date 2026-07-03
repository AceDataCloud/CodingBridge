"""Tests for the local channels config portal (``coding-bridge channels portal``)."""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from coding_bridge.channels.config import (
    ChannelsConfig,
    WeChatInstanceConfig,
    load_channels_config,
)
from coding_bridge.channels.portal import (
    PortalError,
    PortalService,
    _make_handler,
    dump_channels_toml,
)
from coding_bridge.config import Settings


def _settings(tmp_path):
    s = Settings.from_env()
    s.config_dir = tmp_path
    return s


def _inst(**over):
    base = {
        "instance_id": "beijing",
        "base_url": "http://gw.example:8000",
        "token_env": "WT",
        "enabled": True,
        "default_provider": "claude",
        "trigger_prefix": "",
        "allowed_senders": ("CQCcqc",),
        "rate_limit_per_min": 6,
        "dedup_window_seconds": 300.0,
    }
    base.update(over)
    return WeChatInstanceConfig(**base)


def _mock_transport():
    calls = {"contacts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/account":
            return httpx.Response(
                200,
                json={
                    "wechat_id": "AceDataCloud",
                    "nickname": "Ace",
                    "avatar_url": None,
                    "status": "online",
                },
            )
        if p == "/api/conversations":
            return httpx.Response(
                200,
                json={
                    "conversations": [
                        {"id": "g1@chatroom", "name": "Team", "type": "group"},
                        {"id": "CQCcqc", "name": "崔庆才", "type": "private"},
                    ]
                },
            )
        if p == "/api/contacts":
            calls["contacts"] += 1
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "total": 2,
                        "contacts": [
                            {"wechat_id": "CQCcqc", "nickname": "崔庆才", "remark": None},
                            {"wechat_id": "lzf88", "nickname": "Bob", "remark": "friend"},
                        ],
                    },
                )
            return httpx.Response(200, json={"total": 2, "contacts": []})
        if p == "/api/auth/status":
            return httpx.Response(200, json={"logged_in": True})
        return httpx.Response(404, json={"error": "nope"})

    return httpx.MockTransport(handler), calls


def _service(tmp_path, monkeypatch, *, seed=True):
    monkeypatch.setenv("WT", "gw-secret")
    s = _settings(tmp_path)
    if seed:
        (tmp_path / "channels.toml").write_text(
            dump_channels_toml(ChannelsConfig(wechat=(_inst(),))), encoding="utf-8"
        )
    transport, calls = _mock_transport()
    svc = PortalService(s, client=httpx.Client(transport=transport))
    return svc, calls


# --------------------------------------------------------------------------- #
# TOML serialization
# --------------------------------------------------------------------------- #


def test_toml_roundtrip(tmp_path):
    cfg = ChannelsConfig(
        wechat=(
            _inst(instance_id="a", trigger_prefix="", allowed_senders=("x", "y")),
            _inst(instance_id="b", trigger_prefix="/ask ", token_env="WT2", enabled=False),
        )
    )
    path = tmp_path / "channels.toml"
    path.write_text(dump_channels_toml(cfg), encoding="utf-8")
    back = load_channels_config(path)
    assert [i.instance_id for i in back.wechat] == ["a", "b"]
    a, b = back.wechat
    assert a.trigger_prefix == "" and a.allowed_senders == ("x", "y")
    assert b.trigger_prefix == "/ask " and b.enabled is False
    assert a.dedup_window_seconds == 300.0


def test_toml_escapes_quotes(tmp_path):
    cfg = ChannelsConfig(wechat=(_inst(allowed_senders=('weird"id', "a\\b"),),))
    path = tmp_path / "channels.toml"
    path.write_text(dump_channels_toml(cfg), encoding="utf-8")
    back = load_channels_config(path)
    assert back.wechat[0].allowed_senders == ('weird"id', "a\\b")


# --------------------------------------------------------------------------- #
# PortalService — config
# --------------------------------------------------------------------------- #


def test_public_config_hides_token(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch)
    pub = svc.public_config()
    assert pub["instances"][0]["token_source"] == {"kind": "env", "ref": "WT"}
    assert pub["instances"][0]["token_resolvable"] is True
    assert pub["instances"][0]["free_form"] is True
    # the secret value is never present anywhere in the payload
    assert "gw-secret" not in str(pub)
    svc.close()


def test_save_writes_and_validates(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch, seed=False)
    result = svc.save(
        [
            {
                "instance_id": "beijing",
                "base_url": "http://gw.example:8000",
                "token_env": "WT",
                "enabled": True,
                "default_provider": "claude",
                "free_form": False,
                "trigger_prefix": "/ask ",
                "allowed_senders": ["CQCcqc", ""],  # empty dropped
                "rate_limit_per_min": 3,
            }
        ]
    )
    inst = result["instances"][0]
    assert inst["trigger_prefix"] == "/ask " and inst["allowed_senders"] == ["CQCcqc"]
    assert inst["rate_limit_per_min"] == 3
    # persisted + reloadable by the real loader
    reloaded = load_channels_config(tmp_path / "channels.toml")
    assert reloaded.wechat[0].allowed_senders == ("CQCcqc",)


def test_save_free_form_clears_prefix(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch, seed=False)
    result = svc.save(
        [
            {
                "instance_id": "b",
                "base_url": "https://gw.example",
                "token_env": "WT",
                "free_form": True,
                "trigger_prefix": "/ask ",  # ignored because free_form wins
            }
        ]
    )
    assert result["instances"][0]["trigger_prefix"] == ""
    assert result["instances"][0]["free_form"] is True
    svc.close()


def test_save_rejects_bad_base_url(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch, seed=False)
    with pytest.raises(PortalError) as ei:
        svc.save([{"instance_id": "x", "base_url": "ftp://nope", "token_env": "WT"}])
    assert ei.value.status == 400
    svc.close()


def test_save_write_failure_is_500_not_traceback(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch, seed=False)

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr("coding_bridge.channels.portal._write_atomic", boom)
    with pytest.raises(PortalError) as ei:
        svc.save([{"instance_id": "x", "base_url": "http://gw", "token_env": "WT"}])
    assert ei.value.status == 500
    # the safe message names the error kind, never the path or a traceback
    assert "PermissionError" in ei.value.message and "channels.toml" not in ei.value.message
    svc.close()


# --------------------------------------------------------------------------- #
# PortalService — gateway proxy
# --------------------------------------------------------------------------- #


def test_account_and_groups(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch)
    assert svc.account("beijing")["nickname"] == "Ace"
    groups = svc.groups("beijing")
    assert [g["name"] for g in groups] == ["Team"]  # private conv excluded
    svc.close()


def test_contacts_search_and_cache(tmp_path, monkeypatch):
    svc, calls = _service(tmp_path, monkeypatch)
    hits = svc.search_contacts("beijing", "崔", 10)
    assert [c["wechat_id"] for c in hits] == ["CQCcqc"]
    assert all("_haystack" not in c for c in hits)
    # second search is served from cache (no extra gateway fetch)
    before = calls["contacts"]
    svc.search_contacts("beijing", "bob", 10)
    assert calls["contacts"] == before
    svc.close()


def test_unknown_instance_404(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch)
    with pytest.raises(PortalError) as ei:
        svc.account("ghost")
    assert ei.value.status == 404
    svc.close()


def test_token_unresolvable_marks_public(tmp_path, monkeypatch):
    monkeypatch.delenv("WT", raising=False)
    s = _settings(tmp_path)
    (tmp_path / "channels.toml").write_text(
        dump_channels_toml(ChannelsConfig(wechat=(_inst(),))), encoding="utf-8"
    )
    svc = PortalService(s, client=httpx.Client(transport=_mock_transport()[0]))
    assert svc.public_config()["instances"][0]["token_resolvable"] is False
    svc.close()


# --------------------------------------------------------------------------- #
# HTTP layer — token + host gating (live loopback server)
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path, monkeypatch)
    token = "portal-tok"
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(svc, token, port))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        httpd.shutdown()
        httpd.server_close()
        svc.close()


def test_index_served_with_token(live):
    base, token = live
    r = httpx.get(base + "/", timeout=5)
    assert r.status_code == 200
    assert token in r.text and "Channels" in r.text
    assert "__PORTAL_TOKEN__" not in r.text


def test_api_requires_token(live):
    base, _ = live
    assert httpx.get(base + "/api/config", timeout=5).status_code == 401


def test_api_with_header_token(live):
    base, token = live
    r = httpx.get(base + "/api/config", headers={"X-Portal-Token": token}, timeout=5)
    assert r.status_code == 200
    assert r.json()["instances"][0]["instance_id"] == "beijing"


def test_api_with_query_token(live):
    base, token = live
    r = httpx.get(base + f"/api/config?token={token}", timeout=5)
    assert r.status_code == 200


def test_bad_host_rejected(live):
    base, token = live
    r = httpx.get(
        base + "/api/config",
        headers={"Host": "evil.example", "X-Portal-Token": token},
        timeout=5,
    )
    assert r.status_code == 403


def test_post_saves(live):
    base, token = live
    body = {
        "instances": [
            {
                "instance_id": "beijing",
                "base_url": "http://gw.example:8000",
                "token_env": "WT",
                "enabled": True,
                "free_form": True,
                "allowed_senders": ["CQCcqc", "newguy"],
            }
        ]
    }
    r = httpx.post(
        base + "/api/config", headers={"X-Portal-Token": token}, json=body, timeout=5
    )
    assert r.status_code == 200
    assert set(r.json()["instances"][0]["allowed_senders"]) == {"CQCcqc", "newguy"}
