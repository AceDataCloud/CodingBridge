import json

import pytest

from coding_bridge import history, protocol
from coding_bridge.config import Settings
from coding_bridge.connection import BridgeConnection
from coding_bridge.protocol import Action, Event

CODEX_SID = "11111111-1111-1111-1111-111111111111"
COPILOT_SID = "44444444-4444-4444-4444-444444444444"


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _seed_claude(root):
    records = [
        {
            "type": "user",
            "cwd": "/home/me/proj",
            "gitBranch": "main",
            "timestamp": "2025-06-01T10:00:00.000Z",
            "message": {"role": "user", "content": "hello world"},
        },
        {
            "type": "assistant",
            "timestamp": "2025-06-01T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hi there"},
                    {"type": "tool_use", "name": "Bash", "id": "tu1", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2025-06-01T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": "file.txt"}
                ],
            },
        },
    ]
    _write_jsonl(root / "-home-me-proj" / "sid-claude.jsonl", records)


def _seed_codex(root, index_path):
    records = [
        {
            "type": "session_meta",
            "timestamp": "2025-06-02T09:00:00.000Z",
            "payload": {
                "id": CODEX_SID,
                "cwd": "/home/me/cx",
                "git": {"branch": "dev"},
                "model_provider": "openai",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "INSTRUCTIONS"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:02.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "build me a thing"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:03.000Z",
            "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:04.000Z",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call1",
                "arguments": '{"cmd": "ls"}',
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:05.000Z",
            "payload": {"type": "function_call_output", "call_id": "call1", "output": "file.txt"},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:06.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    ]
    name = f"rollout-2025-06-02T09-00-00-{CODEX_SID}.jsonl"
    _write_jsonl(root / "2025" / "06" / "02" / name, records)
    index_path.write_text(
        json.dumps(
            {
                "id": CODEX_SID,
                "thread_name": "My Codex Thread",
                "updated_at": "2025-06-02T09:10:00.000Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _patch_roots(monkeypatch, tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    codex_index = tmp_path / "session_index.jsonl"
    copilot_root = tmp_path / "copilot"
    _seed_claude(claude_root)
    _seed_codex(codex_root, codex_index)
    _seed_copilot(copilot_root)
    monkeypatch.setattr(history, "CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(history, "CODEX_ROOT", codex_root)
    monkeypatch.setattr(history, "CODEX_INDEX", codex_index)
    monkeypatch.setattr(history, "COPILOT_ROOT", copilot_root)
    monkeypatch.setattr(history, "VSCODE_CHAT_ROOTS", [tmp_path / "vscode"])


def _seed_copilot(root):
    session_dir = root / COPILOT_SID
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "workspace.yaml").write_text(
        "\n".join(
            [
                f"id: {COPILOT_SID}",
                "cwd: /home/me/copilot",
                "client_name: github/acp",
                "name: My Copilot Thread",
                "user_named: false",
                "summary_count: 0",
                "created_at: 2025-06-03T08:00:00.000Z",
                "updated_at: 2025-06-03T08:10:00.000Z",
                "",
            ]
        ),
        encoding="utf-8",
    )
    records = [
        {
            "type": "session.start",
            "timestamp": "2025-06-03T08:00:00.000Z",
            "data": {"sessionId": COPILOT_SID, "context": {"cwd": "/home/me/copilot"}},
        },
        {
            "type": "session.model_change",
            "timestamp": "2025-06-03T08:00:01.000Z",
            "data": {"newModel": "gpt-5.4"},
        },
        {
            "type": "user.message",
            "timestamp": "2025-06-03T08:00:02.000Z",
            "data": {"content": "fix the bridge"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-03T08:00:03.000Z",
            "data": {
                "content": "",
                "toolRequests": [
                    {"toolCallId": "call1", "name": "bash", "arguments": {"command": "ls"}}
                ],
            },
        },
        {
            "type": "tool.execution_start",
            "timestamp": "2025-06-03T08:00:04.000Z",
            "data": {"toolCallId": "call1", "toolName": "bash", "arguments": {"command": "ls"}},
        },
        {
            "type": "tool.execution_complete",
            "timestamp": "2025-06-03T08:00:05.000Z",
            "data": {
                "toolCallId": "call1",
                "success": True,
                "result": {"content": "file.txt"},
            },
        },
        {
            "type": "assistant.message",
            "timestamp": "2025-06-03T08:00:06.000Z",
            "data": {"content": "done", "model": "gpt-5.4"},
        },
    ]
    _write_jsonl(session_dir / "events.jsonl", records)


def test_list_sessions_includes_both_providers(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    sessions = history.list_sessions()
    by_provider = {s["provider"]: s for s in sessions}
    assert set(by_provider) == {"claude", "codex", "copilot"}
    assert by_provider["claude"]["title"] == "hello world"
    assert by_provider["claude"]["message_count"] == 3
    assert by_provider["codex"]["title"] == "My Codex Thread"
    # developer-role messages are not counted as conversation.
    assert by_provider["codex"]["message_count"] == 2
    assert by_provider["copilot"]["title"] == "My Copilot Thread"
    # assistant tool-request scaffolding does not count as a chat message.
    assert by_provider["copilot"]["message_count"] == 2
    timestamps = [s["updated_at"] for s in sessions]
    assert timestamps == sorted(timestamps, reverse=True)


def test_read_claude_session_normalises_blocks(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    detail = history.read_session("claude", "sid-claude")
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["prompt", "thinking", "text", "tool_use", "tool_result"]
    assert detail["cwd"] == "/home/me/proj"
    assert detail["git_branch"] == "main"
    assert detail["model"] == "claude-sonnet"
    tool_use = detail["events"][3]
    assert tool_use["tool"] == "Bash"
    assert tool_use["input"] == {"command": "ls"}


def test_read_codex_session_filters_and_maps(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    detail = history.read_session("codex", CODEX_SID)
    kinds = [e["kind"] for e in detail["events"]]
    # developer instructions dropped; tool calls mapped to tool_use/tool_result.
    assert kinds == ["prompt", "thinking", "tool_use", "tool_result", "text"]
    assert detail["cwd"] == "/home/me/cx"
    assert detail["git_branch"] == "dev"
    tool_use = detail["events"][2]
    assert tool_use["tool"] == "exec_command"
    assert tool_use["input"] == {"cmd": "ls"}
    assert detail["events"][3]["content"] == "file.txt"


def test_read_copilot_session_filters_and_maps(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    detail = history.read_session("copilot", COPILOT_SID)
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["prompt", "tool_use", "tool_result", "text"]
    assert detail["cwd"] == "/home/me/copilot"
    assert detail["model"] == "gpt-5.4"
    tool_use = detail["events"][1]
    assert tool_use["tool"] == "bash"
    assert tool_use["input"] == {"command": "ls"}
    assert detail["events"][2]["content"] == "file.txt"


def test_is_context_noise_detects_injections():
    noisy = [
        "# AGENTS.md instructions for /x",
        "# Copilot Instructions\n\nstuff",
        "# Context from my IDE setup:\n",
        "<environment_context>\n  <cwd>/x</cwd>",
        "<cwd>/x</cwd>",
        "<ide_opened_file>The user opened the file /x</ide_opened_file>",
        "The user opened the file /x in the IDE.",
        "Caveat: The messages below were generated by the user",
        "The following is the Codex agent history",
        "",
    ]
    for text in noisy:
        assert history._is_context_noise(text), text
    for text in ("你好", "build me a thing", "fix the bug in foo.py"):
        assert not history._is_context_noise(text), text


def test_unwrap_ide_context_extracts_request():
    wrapper = (
        "# Context from my IDE setup:\n\n"
        "## Open tabs:\n- a.py\n\n"
        "## My request for Codex:\nplease refactor"
    )
    assert history._unwrap_user_text(wrapper) == "please refactor"
    assert history._unwrap_user_text("# Context from my IDE setup:\nno request") == ""
    assert history._unwrap_user_text("just a normal prompt") == "just a normal prompt"


def test_unwrap_copilot_transformed_prompt():
    # Copilot-specific tag stripping lives in _copilot_user_text, not the shared
    # _unwrap_user_text (which codex also uses), so codex prompts aren't truncated.
    wrapper = (
        "<current_datetime>2026-07-02T00:54:54.203+08:00</current_datetime>\n\n"
        "fix the bridge\n\n"
        "<system_reminder>\n<sql_tables>Available tables: todos</sql_tables>\n</system_reminder>"
    )
    assert history._copilot_user_text({"transformedContent": wrapper}) == "fix the bridge"
    # Shared unwrapper must leave a codex prompt ending in a tag intact.
    assert history._unwrap_user_text(wrapper) == wrapper.strip()


def test_copilot_read_tolerates_non_dict_event_data(monkeypatch, tmp_path):
    # Malformed transcripts (data as a list/None) must not crash the reader.
    copilot_root = tmp_path / "copilot"
    session_dir = copilot_root / COPILOT_SID
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "workspace.yaml").write_text(f"id: {COPILOT_SID}\n", encoding="utf-8")
    (session_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "user.message", "data": ["oops", "not", "a", "dict"]}),
                json.dumps({"type": "assistant.message", "data": None}),
                json.dumps({"type": "user.message", "data": {"content": "real prompt"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "COPILOT_ROOT", copilot_root)
    detail = history.read_session("copilot", COPILOT_SID)
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["real prompt"]
    summaries = history.list_sessions()
    assert any(s["session_id"] == COPILOT_SID for s in summaries)


def test_copilot_path_rejects_traversal(monkeypatch, tmp_path):
    copilot_root = tmp_path / "copilot"
    copilot_root.mkdir(parents=True, exist_ok=True)
    # A stray events.jsonl one level up must NOT be reachable via `..`.
    (copilot_root.parent / "events.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(history, "COPILOT_ROOT", copilot_root)
    assert history._copilot_path("..") is None
    assert history._copilot_path(".") is None
    with pytest.raises(FileNotFoundError):
        history.read_session("copilot", "..")


def test_copilot_falls_back_to_transformed_content(monkeypatch, tmp_path):
    copilot_root = tmp_path / "copilot"
    session_dir = copilot_root / COPILOT_SID
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "workspace.yaml").write_text(f"id: {COPILOT_SID}\n", encoding="utf-8")
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "type": "user.message",
                "timestamp": "2025-06-03T08:00:02.000Z",
                "data": {
                    "content": "",
                    "transformedContent": (
                        "<current_datetime>2026-07-02T00:54:54.203+08:00</current_datetime>\n\n"
                        "show me recent sessions\n\n"
                        "<system_reminder>\n"
                        "<sql_tables>Available tables: todos</sql_tables>\n"
                        "</system_reminder>"
                    ),
                },
            }
        ],
    )
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path / "none-claude")
    monkeypatch.setattr(history, "CODEX_ROOT", tmp_path / "none-codex")
    monkeypatch.setattr(history, "CODEX_INDEX", tmp_path / "none.jsonl")
    monkeypatch.setattr(history, "COPILOT_ROOT", copilot_root)
    monkeypatch.setattr(history, "VSCODE_CHAT_ROOTS", [tmp_path / "none-vscode"])
    copilot = next(s for s in history.list_sessions() if s["provider"] == "copilot")
    assert copilot["title"] == "show me recent sessions"
    detail = history.read_session("copilot", COPILOT_SID)
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["show me recent sessions"]


def test_claude_skips_ide_noise_block_uses_real_prompt(monkeypatch, tmp_path):
    claude_root = tmp_path / "claude"
    records = [
        {
            "type": "user",
            "cwd": "/home/me/p",
            "timestamp": "2025-06-01T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<ide_opened_file>The user opened the file /x/README.md "
                            "in the IDE.</ide_opened_file>"
                        ),
                    },
                    {"type": "text", "text": "Hello, what's in this project?"},
                ],
            },
        },
    ]
    _write_jsonl(claude_root / "-home-me-p" / "sid-ide.jsonl", records)
    monkeypatch.setattr(history, "CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(history, "CODEX_ROOT", tmp_path / "none")
    monkeypatch.setattr(history, "CODEX_INDEX", tmp_path / "none.jsonl")
    claude = next(s for s in history.list_sessions() if s["provider"] == "claude")
    assert claude["title"] == "Hello, what's in this project?"
    detail = history.read_session("claude", "sid-ide")
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["Hello, what's in this project?"]


def test_codex_skips_injected_context_for_title(monkeypatch, tmp_path):
    codex_root = tmp_path / "codex"
    sid = "22222222-2222-2222-2222-222222222222"
    records = [
        {
            "type": "session_meta",
            "timestamp": "2025-06-02T09:00:00.000Z",
            "payload": {"id": sid, "cwd": "/c", "git": {"branch": "m"}},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>/c</cwd>"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:02.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /c\n\nx"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:03.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "actually do the thing"}],
            },
        },
    ]
    name = f"rollout-2025-06-02T09-00-00-{sid}.jsonl"
    _write_jsonl(codex_root / "2025" / "06" / "02" / name, records)
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path / "none")
    monkeypatch.setattr(history, "CODEX_ROOT", codex_root)
    monkeypatch.setattr(history, "CODEX_INDEX", tmp_path / "none.jsonl")
    codex = next(s for s in history.list_sessions() if s["provider"] == "codex")
    assert codex["title"] == "actually do the thing"
    detail = history.read_session("codex", sid)
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["actually do the thing"]


def test_codex_unwraps_ide_request_and_drops_dump(monkeypatch, tmp_path):
    codex_root = tmp_path / "codex"
    sid = "33333333-3333-3333-3333-333333333333"
    wrapper = (
        "# Context from my IDE setup:\n\n"
        "## Active selection of the file:\nSECRET_TOKEN=abc123\n\n"
        "## Open tabs:\n- a.py\n\n"
        "## My request for Codex:\nsummarise the open file"
    )
    records = [
        {
            "type": "session_meta",
            "timestamp": "2025-06-02T09:00:00.000Z",
            "payload": {"id": sid, "cwd": "/c"},
        },
        {
            "type": "response_item",
            "timestamp": "2025-06-02T09:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": wrapper}],
            },
        },
    ]
    name = f"rollout-2025-06-02T09-00-00-{sid}.jsonl"
    _write_jsonl(codex_root / "2025" / "06" / "02" / name, records)
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path / "none")
    monkeypatch.setattr(history, "CODEX_ROOT", codex_root)
    monkeypatch.setattr(history, "CODEX_INDEX", tmp_path / "none.jsonl")
    detail = history.read_session("codex", sid)
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["summarise the open file"]
    dumped = json.dumps(detail)
    assert "SECRET_TOKEN" not in dumped
    assert "Open tabs" not in dumped


def test_read_session_rejects_path_traversal(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    for bad in ("../secret", "a/b", "..%2f"):
        try:
            history.read_session("claude", bad)
        except FileNotFoundError:
            continue
        raise AssertionError(f"expected FileNotFoundError for {bad!r}")


def test_unknown_provider_raises(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    try:
        history.read_session("bogus", "sid-claude")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown provider")


# --- dispatch integration --------------------------------------------------
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _new_conn():
    settings = Settings(bridge_url="https://bridge.test", permission_timeout=0.2)
    conn = BridgeConnection(settings, "node_tok")
    conn._ws = _FakeWS()
    return conn


def _payloads(conn, event):
    return [
        m["payload"]
        for m in conn._ws.sent
        if m.get("type") == protocol.NODE_TO_BROWSER and m["payload"].get("event") == event
    ]


async def test_dispatch_history_list_emits_snapshot(monkeypatch):
    monkeypatch.setattr(
        history, "list_sessions", lambda limit=200: [{"provider": "claude", "session_id": "s"}]
    )
    conn = _new_conn()
    await conn._dispatch({"action": Action.HISTORY_LIST})
    snapshots = _payloads(conn, Event.HISTORY_SNAPSHOT)
    assert snapshots and snapshots[0]["sessions"][0]["session_id"] == "s"


async def test_dispatch_history_get_emits_detail(monkeypatch):
    monkeypatch.setattr(
        history,
        "read_session",
        lambda provider, session_id: {"provider": provider, "events": [], "title": "t"},
    )
    conn = _new_conn()
    await conn._dispatch(
        {"action": Action.HISTORY_GET, "provider": "claude", "session_id": "sid-claude"}
    )
    details = _payloads(conn, Event.HISTORY_DETAIL)
    assert details and details[0]["provider"] == "claude"
    assert details[0]["session_id"] == "sid-claude"


async def test_dispatch_history_get_folds_in_sidecar(monkeypatch, tmp_path):
    from coding_bridge import session_meta

    # Transcript carries cwd/model but not effort/permission_mode.
    monkeypatch.setattr(
        history,
        "read_session",
        lambda provider, session_id: {
            "provider": provider, "events": [], "title": "t", "cwd": "/repo", "model": "opus"
        },
    )
    settings = Settings(bridge_url="https://bridge.test", config_dir=tmp_path)
    conn = BridgeConnection(settings, "node_tok")
    conn._ws = _FakeWS()
    session_meta.save(tmp_path, "sid-claude", permission_mode="plan", effort="high")
    await conn._dispatch(
        {"action": Action.HISTORY_GET, "provider": "claude", "session_id": "sid-claude"}
    )
    detail = _payloads(conn, Event.HISTORY_DETAIL)[0]
    assert detail["permission_mode"] == "plan"
    assert detail["effort"] == "high"
    assert detail["cwd"] == "/repo"  # transcript stays authoritative
    assert detail["model"] == "opus"


async def test_dispatch_history_get_requires_params():
    conn = _new_conn()
    await conn._dispatch({"action": Action.HISTORY_GET})
    errors = _payloads(conn, Event.SESSION_ERROR)
    assert errors and "required" in errors[0]["message"]


VSCODE_SID = "55555555-5555-5555-5555-555555555555"


def _seed_vscode_chat(storage_root):
    ws = storage_root / "abc123hash"
    (ws / "chatSessions").mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": "file:///Users/me/My%20Proj"}), encoding="utf-8"
    )
    # v3 delta log: header line then a kind:2 line carrying the request objects.
    records = [
        {"kind": 0, "v": {"version": 3, "sessionId": VSCODE_SID, "requests": []}},
        {
            "kind": 2,
            "v": [
                {
                    "requestId": "req_1",
                    "timestamp": 1750000000000,
                    "modelId": "copilot/claude-opus-4.8",
                    "message": {"text": "fix the parser"},
                    "response": [
                        {"kind": "thinking", "value": "let me look", "id": "t0"},
                        {"value": "Here is the fix.", "supportThemeIcons": False},
                        {
                            "kind": "toolInvocationSerialized",
                            "toolId": "copilot_readFile",
                            "toolCallId": "call_1",
                            "invocationMessage": {"value": "Reading file"},
                            "isComplete": True,
                        },
                    ],
                }
            ],
        },
    ]
    _write_jsonl(ws / "chatSessions" / f"{VSCODE_SID}.jsonl", records)


def test_vscode_chat_list_and_read(monkeypatch, tmp_path):
    storage = tmp_path / "workspaceStorage"
    _seed_vscode_chat(storage)
    monkeypatch.setattr(history, "VSCODE_CHAT_ROOTS", [storage])
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path / "none1")
    monkeypatch.setattr(history, "CODEX_ROOT", tmp_path / "none2")
    monkeypatch.setattr(history, "COPILOT_ROOT", tmp_path / "none3")

    sessions = history.list_sessions()
    vs = [s for s in sessions if s["session_id"] == VSCODE_SID]
    assert len(vs) == 1
    s = vs[0]
    assert s["provider"] == "copilot"  # surfaced as ordinary Copilot, no "vscode"
    assert "origin" not in s
    assert s["title"] == "fix the parser"
    assert s["cwd"] == "/Users/me/My Proj"  # file:// unquoted

    detail = history.read_session("copilot", VSCODE_SID)
    assert "origin" not in detail
    assert detail["model"] == "copilot/claude-opus-4.8"
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["prompt", "thinking", "text", "tool_use"]
    assert detail["events"][0]["text"] == "fix the parser"
    assert detail["events"][2]["text"] == "Here is the fix."
    assert detail["events"][3]["tool"] == "copilot_readFile"


def test_vscode_chat_path_rejects_traversal(monkeypatch, tmp_path):
    storage = tmp_path / "workspaceStorage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(history, "VSCODE_CHAT_ROOTS", [storage])
    monkeypatch.setattr(history, "COPILOT_ROOT", tmp_path / "none")
    assert history._vscode_chat_path("..") is None
    assert history._vscode_chat_path(".") is None
    with pytest.raises(FileNotFoundError):
        history.read_session("copilot", "..")


def test_vscode_chat_tolerates_malformed(monkeypatch, tmp_path):
    storage = tmp_path / "workspaceStorage"
    ws = storage / "h" / "chatSessions"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / f"{VSCODE_SID}.jsonl").write_text(
        "\n".join(
            [
                "not json",
                json.dumps({"kind": 1, "v": "just a string"}),
                json.dumps({"kind": 2, "v": [{"message": ["bad"], "response": []}]}),
                json.dumps(
                    {
                        "kind": 2,
                        "v": [{"requestId": "r", "message": {"text": "real"}, "response": []}],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "VSCODE_CHAT_ROOTS", [storage])
    monkeypatch.setattr(history, "COPILOT_ROOT", tmp_path / "none")
    detail = history.read_session("copilot", VSCODE_SID)
    prompts = [e["text"] for e in detail["events"] if e["kind"] == "prompt"]
    assert prompts == ["real"]
