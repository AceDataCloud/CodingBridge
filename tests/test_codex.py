import pytest

from coding_bridge.config import Settings
from coding_bridge.protocol import Event
from coding_bridge.providers.codex import CodexProvider, _codex_effort


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
    # The real thread id is announced so the node/browser re-key to it, then
    # subsequent events carry it as the canonical session id.
    assert events == [
        {"event": Event.SESSION_IDENTIFIED, "session_id": "s1", "sdk_session_id": "abc-123"}
    ]
    assert provider._session_id == "abc-123"


async def test_thread_started_no_identity_when_id_matches():
    """Resuming opens under the real id already, so no rename is announced."""
    settings = Settings(bridge_url="https://bridge.test", default_cwd="/work")
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    provider = CodexProvider("abc-123", emit, lambda *a: "deny", settings)
    await provider._handle_event({"type": "thread.started", "thread_id": "abc-123"})
    assert events == []
    assert provider._session_id == "abc-123"


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
    from coding_bridge.providers.codex import _DEFAULT_SANDBOX, _SANDBOX_BY_MODE

    assert _SANDBOX_BY_MODE.get(mode, _DEFAULT_SANDBOX) == sandbox


def test_build_argv_new_session():
    provider, _ = _provider()
    provider._sandbox = "workspace-write"
    provider._model = "gpt-5-codex"
    provider._effort = "high"
    argv = provider._build_argv("do it", resume=False, image_paths=[])
    assert argv[:2] == ["codex", "exec"]
    assert "resume" not in argv
    assert "--json" in argv
    # Fresh `codex exec` accepts -s/--sandbox.
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert "model_reasoning_effort=high" in argv
    # `--` guards the prompt (variadic -i / leading-dash) and it stays last.
    assert argv[-2:] == ["--", "do it"]


def test_build_argv_resume_uses_thread_id():
    provider, _ = _provider()
    provider._sandbox = "workspace-write"
    provider._thread_id = "thread-9"
    argv = provider._build_argv("more", resume=True, image_paths=[])
    assert argv[:3] == ["codex", "exec", "resume"]
    # `codex exec resume` rejects -s; sandbox must ride on a -c config override.
    assert "-s" not in argv
    assert '-c' in argv and 'sandbox_mode="workspace-write"' in argv
    # SESSION_ID then PROMPT come after `--`, in that order.
    assert argv[-3:] == ["--", "thread-9", "more"]


def test_build_argv_resume_protects_dash_prompt_and_images():
    provider, _ = _provider()
    provider._sandbox = "read-only"
    provider._thread_id = "t-7"
    argv = provider._build_argv("-s is literal text", resume=True, image_paths=["/tmp/a.png"])
    assert "-s" not in argv  # the only -s-looking token is inside the prompt, after --
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["t-7", "-s is literal text"]
    assert argv[argv.index("-i") + 1] == "/tmp/a.png"


async def test_send_applies_model_effort_and_sandbox_overrides():
    provider, _ = _provider()
    provider._thread_id = "t1"
    captured: dict = {}

    async def fake_run_turn(prompt, *, resume, images=None, attachments=None):
        captured["resume"] = resume

    provider._run_turn = fake_run_turn  # type: ignore[method-assign]
    await provider.send("go", model="gpt-5", effort="high", permission_mode="plan")
    assert provider._model == "gpt-5"
    assert provider._effort == "high"
    assert provider._sandbox == "read-only"
    assert captured["resume"] is True



def test_build_argv_includes_image_args():
    provider, _ = _provider()
    provider._sandbox = "workspace-write"
    argv = provider._build_argv("look", resume=False, image_paths=["/tmp/a.png", "/tmp/b.png"])
    assert argv.count("-i") == 2
    idx = [i for i, v in enumerate(argv) if v == "-i"]
    assert argv[idx[0] + 1] == "/tmp/a.png"
    assert argv[idx[1] + 1] == "/tmp/b.png"
    assert argv[-1] == "look"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("max", "high"),
        ("ultra-high", "high"),
        ("low", "low"),
        ("medium", "medium"),
        ("xhigh", "xhigh"),
        ("minimal", "minimal"),
        ("bogus", None),
        (None, None),
    ],
)
def test_codex_effort_normalization(raw, expected):
    assert _codex_effort(raw) == expected
