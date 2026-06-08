import pytest

from coding_bridge_agent.config import Settings
from coding_bridge_agent.protocol import Event
from coding_bridge_agent.providers.codex import CodexProvider, _codex_effort


def _provider():
    settings = Settings(bridge_url="https://bridge.test", default_cwd="/work")
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(tool, input_data, ctx):
        return "deny"

    return CodexProvider("s1", emit, ask, settings), events


async def _feed(provider, events, obj):
    await provider._handle_event(obj)
    return events


async def test_thread_started_captures_id():
    provider, events = _provider()
    await provider._handle_event({"type": "thread.started", "thread_id": "abc-123"})
    assert provider._thread_id == "abc-123"
    assert events == []


async def test_agent_message_completed_emits_text():
    provider, events = _provider()
    await provider._handle_event(
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "hello"}}
    )
    assert events[-1]["event"] == Event.SESSION_TEXT
    assert events[-1]["text"] == "hello"


async def test_reasoning_completed_emits_thinking():
    provider, events = _provider()
    await provider._handle_event(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking..."}}
    )
    assert events[-1]["event"] == Event.SESSION_THINKING
    assert events[-1]["text"] == "thinking..."


async def test_command_execution_emits_tool_use_and_result():
    provider, events = _provider()
    await provider._handle_event(
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "ls"}}
    )
    assert events[-1]["event"] == Event.SESSION_TOOL_USE
    assert events[-1]["input"] == {"command": "ls"}
    await provider._handle_event(
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "aggregated_output": "a\nb",
                "exit_code": 0,
            },
        }
    )
    assert events[-1]["event"] == Event.SESSION_TOOL_RESULT
    assert events[-1]["is_error"] is False


async def test_command_failure_marks_error():
    provider, events = _provider()
    await provider._handle_event(
        {
            "type": "item.completed",
            "item": {"id": "c1", "type": "command_execution", "exit_code": 2},
        }
    )
    assert events[-1]["is_error"] is True


async def test_turn_completed_emits_result():
    provider, events = _provider()
    ended = await provider._handle_event({"type": "turn.completed", "usage": {"output_tokens": 5}})
    assert ended is True
    assert events[-1]["event"] == Event.SESSION_RESULT
    assert events[-1]["usage"] == {"output_tokens": 5}


async def test_error_event_emits_session_error():
    provider, events = _provider()
    ended = await provider._handle_event({"type": "turn.failed", "error": {"message": "boom"}})
    assert ended is True
    assert events[-1]["event"] == Event.SESSION_ERROR
    assert events[-1]["message"] == "boom"


async def test_transient_error_is_buffered_not_emitted():
    provider, events = _provider()
    ended = await provider._handle_event(
        {"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}
    )
    assert ended is False
    assert events == []
    assert provider._last_error == "Reconnecting... 2/5 (request timed out)"


@pytest.mark.parametrize(
    "mode,sandbox",
    [
        ("plan", "read-only"),
        ("default", "workspace-write"),
        ("acceptEdits", "workspace-write"),
        ("bypassPermissions", "danger-full-access"),
        ("unknown", "workspace-write"),
    ],
)
def test_permission_mode_maps_to_sandbox(mode, sandbox):
    from coding_bridge_agent.providers.codex import _DEFAULT_SANDBOX, _SANDBOX_BY_MODE

    assert _SANDBOX_BY_MODE.get(mode, _DEFAULT_SANDBOX) == sandbox


def test_build_argv_new_session():
    provider, _ = _provider()
    provider._sandbox = "workspace-write"
    provider._model = "gpt-5-codex"
    provider._effort = "high"
    argv = provider._build_argv("do it", resume=False)
    assert argv[:2] == ["codex", "exec"]
    assert "resume" not in argv
    assert "--json" in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert "model_reasoning_effort=high" in argv
    assert argv[-1] == "do it"


def test_build_argv_resume_uses_thread_id():
    provider, _ = _provider()
    provider._thread_id = "thread-9"
    argv = provider._build_argv("more", resume=True)
    assert argv[:4] == ["codex", "exec", "resume", "thread-9"]
    assert argv[-1] == "more"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("max", "high"),
        ("ultra-high", "high"),
        ("low", "low"),
        ("medium", "medium"),
        ("bogus", None),
        (None, None),
    ],
)
def test_codex_effort_normalization(raw, expected):
    assert _codex_effort(raw) == expected
