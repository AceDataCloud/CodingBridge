import asyncio

import pytest

from coding_bridge.config import Settings
from coding_bridge.permissions import Resolution
from coding_bridge.protocol import Event
from coding_bridge.providers import default_provider_factory
from coding_bridge.providers.copilot import (
    CopilotProvider,
    _AcpError,
    _content_text,
    _copilot_effort,
    _select_option,
    _tool_content_text,
)


def _provider(ask=None):
    settings = Settings(bridge_url="https://bridge.test", default_cwd="/work")
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def default_ask(tool, input_data, ctx):
        return Resolution("deny")

    return CopilotProvider("s1", emit, ask or default_ask, settings), events


async def _update(provider, update):
    await provider._handle_update({"sessionId": "x", "update": update})


# --- session/update event mapping -----------------------------------------
async def test_agent_message_chunk_streams_then_flushes():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
         "content": {"type": "text", "text": "Hel"}},
    )
    await _update(
        provider,
        {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
         "content": {"type": "text", "text": "lo"}},
    )
    assert [e["event"] for e in events] == [Event.SESSION_TEXT_DELTA, Event.SESSION_TEXT_DELTA]
    assert events[0]["text"] == "Hel" and events[0]["id"] == "m1"
    await provider._flush_cur()
    assert events[-1]["event"] == Event.SESSION_TEXT
    assert events[-1]["text"] == "Hello" and events[-1]["id"] == "m1"


async def test_message_id_change_flushes_previous():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
         "content": {"type": "text", "text": "first"}},
    )
    await _update(
        provider,
        {"sessionUpdate": "agent_message_chunk", "messageId": "m2",
         "content": {"type": "text", "text": "second"}},
    )
    committed = [e for e in events if e["event"] == Event.SESSION_TEXT]
    assert committed and committed[0]["text"] == "first"


async def test_thought_chunk_emits_thinking_on_flush():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "pondering"}},
    )
    assert events == []  # thought is buffered, no delta event
    await provider._flush_cur()
    assert events[-1]["event"] == Event.SESSION_THINKING
    assert events[-1]["text"] == "pondering"


async def test_tool_call_emits_tool_use():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Read file",
         "kind": "read", "rawInput": {"path": "/x"}},
    )
    assert events[-1]["event"] == Event.SESSION_TOOL_USE
    assert events[-1]["tool"] == "Read file"
    assert events[-1]["tool_use_id"] == "c1"
    assert events[-1]["input"] == {"path": "/x"}


async def test_tool_call_update_completed_emits_result():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": "done"}}]},
    )
    assert events[-1]["event"] == Event.SESSION_TOOL_RESULT
    assert events[-1]["is_error"] is False
    assert events[-1]["content"] == "done"


async def test_tool_call_update_failed_marks_error():
    provider, events = _provider()
    await _update(provider, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                             "status": "failed"})
    assert events[-1]["event"] == Event.SESSION_TOOL_RESULT
    assert events[-1]["is_error"] is True


async def test_tool_call_update_in_progress_no_result():
    provider, events = _provider()
    await _update(provider, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                             "status": "in_progress"})
    assert events == []


async def test_tool_call_update_flushes_open_text_first():
    provider, events = _provider()
    await _update(
        provider,
        {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
         "content": {"type": "text", "text": "answer"}},
    )
    await _update(provider, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                             "status": "completed"})
    kinds = [e["event"] for e in events]
    # the streamed text is committed before the tool result lands
    assert kinds.index(Event.SESSION_TEXT) < kinds.index(Event.SESSION_TOOL_RESULT)


async def test_usage_update_recorded():
    provider, _ = _provider()
    await _update(provider, {"sessionUpdate": "usage_update", "used": 10, "size": 100})
    assert provider._usage == {"used": 10, "size": 100, "cost": None}


# --- permission option selection ------------------------------------------
def test_select_option_prefers_kind():
    options = [
        {"optionId": "a", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "r", "name": "Reject", "kind": "reject_once"},
    ]
    assert _select_option(options, allow=True) == "a"
    assert _select_option(options, allow=False) == "r"


def test_select_option_allow_keyword_fallback():
    options = [{"optionId": "yes", "name": "Yes go"}, {"optionId": "no", "name": "No stop"}]
    assert _select_option(options, allow=True) == "yes"


def test_select_option_deny_without_kind_cancels():
    # Deny never keyword-matches: a substring like "no" could hit an allow label
    # ("do not ask again"), so a kind-less deny falls back to cancel (None).
    options = [{"optionId": "yes", "name": "Yes go"}, {"optionId": "no", "name": "No stop"}]
    assert _select_option(options, allow=False) is None


def test_select_option_no_match_polarity_default():
    options = [{"optionId": "x", "name": "something"}]
    assert _select_option(options, allow=True) == "x"  # allow falls back to first
    assert _select_option(options, allow=False) is None  # deny falls back to cancel


def test_select_option_empty_is_none():
    assert _select_option([], allow=True) is None
    assert _select_option([], allow=False) is None


# --- permission relay ------------------------------------------------------
async def test_resolve_permission_bypass_auto_allows():
    called = False

    async def ask(tool, input_data, ctx):
        nonlocal called
        called = True
        return Resolution("deny")

    provider, _ = _provider(ask)
    provider._permission_mode = "bypassPermissions"
    options = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]
    out = await provider._resolve_permission({"options": options, "toolCall": {"title": "x"}})
    assert out == {"outcome": {"outcome": "selected", "optionId": "a"}}
    assert called is False  # the approver is never consulted in bypass mode


async def test_resolve_permission_relays_allow():
    async def ask(tool, input_data, ctx):
        assert tool == "Run shell"
        assert input_data == {"cmd": "ls"}
        return Resolution("allow")

    provider, _ = _provider(ask)
    options = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]
    out = await provider._resolve_permission(
        {"options": options, "toolCall": {"title": "Run shell", "rawInput": {"cmd": "ls"}}}
    )
    assert out == {"outcome": {"outcome": "selected", "optionId": "a"}}


async def test_resolve_permission_relays_deny():
    provider, _ = _provider()  # default approver denies
    options = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]
    out = await provider._resolve_permission({"options": options, "toolCall": {"kind": "execute"}})
    assert out == {"outcome": {"outcome": "selected", "optionId": "r"}}


def test_outcome_none_is_cancelled():
    assert CopilotProvider._outcome(None) == {"outcome": {"outcome": "cancelled"}}
    assert CopilotProvider._outcome("o1") == {"outcome": {"outcome": "selected", "optionId": "o1"}}


# --- helpers ---------------------------------------------------------------
def test_copilot_effort_normalizes():
    assert _copilot_effort("max") == "high"
    assert _copilot_effort("minimal") == "low"
    assert _copilot_effort("medium") == "medium"
    assert _copilot_effort("bogus") is None
    assert _copilot_effort("") is None
    assert _copilot_effort(None) is None


def test_content_text():
    assert _content_text({"type": "text", "text": "hi"}) == "hi"
    assert _content_text({"type": "image"}) == ""
    assert _content_text(None) == ""


def test_tool_content_text_flattens():
    content = [
        {"type": "content", "content": {"type": "text", "text": "a"}},
        {"type": "diff", "path": "/f"},
        {"type": "terminal", "terminalId": "t1"},
    ]
    assert _tool_content_text(content) == "a\n[diff] /f\n[terminal t1]"
    assert _tool_content_text(None) is None
    assert _tool_content_text({"type": "content", "content": {"type": "text", "text": "x"}}) == "x"


# --- transport dispatch ----------------------------------------------------
async def test_dispatch_resolves_pending_request():
    provider, _ = _provider()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    provider._pending[1] = fut
    await provider._dispatch({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "abc"}})
    assert fut.result() == {"sessionId": "abc"}


async def test_dispatch_error_fails_future():
    provider, _ = _provider()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    provider._pending[2] = fut
    await provider._dispatch({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "boom"}})
    assert isinstance(fut.exception(), _AcpError)


async def test_dispatch_routes_update_notification():
    provider, events = _provider()
    await provider._dispatch(
        {"jsonrpc": "2.0", "method": "session/update",
         "params": {"update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": "hi"}}}}
    )
    assert events[-1]["event"] == Event.SESSION_TEXT_DELTA


# --- identity + edit + registration ---------------------------------------
async def test_maybe_announce_identity_emits_once():
    provider, events = _provider()
    provider._acp_session_id = "real-id"
    await provider._maybe_announce_identity()
    assert events[-1]["event"] == Event.SESSION_IDENTIFIED
    assert events[-1]["sdk_session_id"] == "real-id"
    assert provider._session_id == "real-id"
    events.clear()
    await provider._maybe_announce_identity()
    assert events == []  # already announced


async def test_no_identity_when_id_matches():
    settings = Settings(bridge_url="https://bridge.test", default_cwd="/work")
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    provider = CopilotProvider("real-id", emit, lambda *a: Resolution("deny"), settings)
    provider._acp_session_id = "real-id"
    await provider._maybe_announce_identity()
    assert events == []


async def test_edit_reports_unsupported():
    provider, events = _provider()
    await provider.edit("redo it", cut_uuid=None)
    assert events[0]["event"] == Event.SESSION_NOTICE
    assert events[0]["code"] == "edit_unsupported"
    assert events[-1]["event"] == Event.SESSION_RESULT


def test_capabilities_lists_copilot():
    from coding_bridge import capabilities

    desc = capabilities.describe(Settings(default_cwd="/work"))
    providers = {p["name"]: p for p in desc["providers"]}
    assert "copilot" in providers
    cop = providers["copilot"]
    assert cop["supports_edit"] is False
    assert cop["allow_custom_model"] is True
    assert cop["models"]  # non-empty static catalog


def test_factory_builds_copilot():
    factory = default_provider_factory(Settings(default_cwd="/w"))

    async def emit(payload):
        ...

    async def ask(*args):
        return Resolution("deny")

    provider = factory("copilot", "s1", emit, ask)
    assert isinstance(provider, CopilotProvider)
    assert provider.name == "copilot"


@pytest.mark.parametrize("mode,auto", [("bypassPermissions", True), ("default", False),
                                       ("plan", False), ("acceptEdits", False)])
async def test_permission_mode_auto_approve(mode, auto):
    asked = False

    async def ask(tool, input_data, ctx):
        nonlocal asked
        asked = True
        return Resolution("allow")

    provider, _ = _provider(ask)
    provider._permission_mode = mode
    options = [{"optionId": "a", "kind": "allow_once"}]
    await provider._resolve_permission({"options": options, "toolCall": {"title": "x"}})
    assert asked is (not auto)


# --- dead-process reconnect (no silent hang between turns) -----------------
async def test_reader_loop_eof_disconnects_and_fails_pending():
    provider, _ = _provider()
    provider._connected = True
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    provider._pending[1] = fut

    class _EOFStream:
        async def readline(self):
            return b""

    class _DeadProc:
        stdout = _EOFStream()
        returncode = 0

    provider._proc = _DeadProc()
    await provider._reader_loop()
    assert provider._connected is False  # next turn reconnects instead of hanging
    assert isinstance(fut.exception(), RuntimeError)


async def test_ensure_proc_reconnects_when_process_dead(monkeypatch):
    provider, _ = _provider()
    provider._connected = True

    class _DeadProc:
        returncode = 1

    provider._proc = _DeadProc()
    calls = []

    async def fake_aclose():
        calls.append("aclose")
        provider._connected = False

    async def fake_spawn(*, resume):
        calls.append(("spawn", resume))

    monkeypatch.setattr(provider, "aclose", fake_aclose)
    monkeypatch.setattr(provider, "_spawn_and_connect", fake_spawn)
    await provider._ensure_proc(resume="sid")
    assert calls == ["aclose", ("spawn", "sid")]


async def test_ensure_proc_noop_when_process_alive(monkeypatch):
    provider, _ = _provider()
    provider._connected = True

    class _AliveProc:
        returncode = None

    provider._proc = _AliveProc()

    async def fake_spawn(*, resume):
        raise AssertionError("must not respawn a live ACP process")

    monkeypatch.setattr(provider, "_spawn_and_connect", fake_spawn)
    await provider._ensure_proc(resume=None)  # returns without respawning
