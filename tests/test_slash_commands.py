"""Slash-command routing: detection plus per-provider handling."""
from coding_bridge.config import Settings
from coding_bridge.providers.base import slash_name
from coding_bridge.providers.claude import ClaudeProvider, _command_from_rejection
from coding_bridge.providers.codex import CodexProvider


def test_slash_name_extracts_command():
    assert slash_name("/status") == "status"
    assert slash_name("/Compact extra args") == "compact"
    assert slash_name("  /context  ") == "context"


def test_slash_name_rejects_non_commands():
    assert slash_name("hello") is None
    assert slash_name("") is None
    assert slash_name("/") is None
    assert slash_name("/Users/qicu/file") is None  # a path, not a command
    assert slash_name("please run /status") is None  # not leading


def test_command_from_rejection():
    assert _command_from_rejection("/status isn't available in this environment.") == "status"
    assert _command_from_rejection("status isn't available in this environment.") == "status"


def _capturing_provider(cls):
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(*_args):
        return "deny"

    return cls("s1", emit, ask, Settings()), events


async def test_claude_known_command_defers_to_sdk():
    provider, events = _capturing_provider(ClaudeProvider)
    provider._known_commands = {"context", "compact"}
    assert await provider._maybe_handle_slash("/context") is False
    assert events == []


async def test_claude_unknown_command_emits_localized_notice():
    provider, events = _capturing_provider(ClaudeProvider)
    provider._known_commands = {"context"}
    assert await provider._maybe_handle_slash("/login") is True
    kinds = [e["event"] for e in events]
    assert "session.notice" in kinds
    assert "session.result" in kinds  # turn is closed so the UI returns to idle
    notice = next(e for e in events if e["event"] == "session.notice")
    assert notice["command"] == "login"
    assert notice["code"] == "slash_unavailable"


async def test_claude_status_is_synthesized_from_server_info():
    provider, events = _capturing_provider(ClaudeProvider)
    provider._known_commands = {"context"}
    provider._server_info = {
        "account": {"email": "a@b.com", "organization": "Org", "subscriptionType": "Max"}
    }
    provider._model = "sonnet"
    assert await provider._maybe_handle_slash("/status") is True
    text_event = next(e for e in events if e["event"] == "session.text")
    assert "Status" in text_event["text"]
    assert "a@b.com" in text_event["text"]
    assert "sonnet" in text_event["text"]


async def test_claude_no_catalog_defers_to_sdk():
    provider, events = _capturing_provider(ClaudeProvider)
    provider._known_commands = set()
    assert await provider._maybe_handle_slash("/status") is False
    assert events == []


async def test_codex_slash_is_intercepted():
    provider, events = _capturing_provider(CodexProvider)
    assert await provider._maybe_handle_slash("/status") is True
    notice = next(e for e in events if e["event"] == "session.notice")
    assert notice["code"] == "slash_codex_unsupported"
    assert any(e["event"] == "session.result" for e in events)


async def test_codex_normal_prompt_is_not_intercepted():
    provider, events = _capturing_provider(CodexProvider)
    assert await provider._maybe_handle_slash("hello world") is False
    assert events == []
