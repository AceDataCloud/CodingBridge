import json

from coding_bridge_agent import history, protocol
from coding_bridge_agent.config import Settings
from coding_bridge_agent.connection import BridgeConnection
from coding_bridge_agent.protocol import Action, Event

CODEX_SID = "11111111-1111-1111-1111-111111111111"


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
    _seed_claude(claude_root)
    _seed_codex(codex_root, codex_index)
    monkeypatch.setattr(history, "CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(history, "CODEX_ROOT", codex_root)
    monkeypatch.setattr(history, "CODEX_INDEX", codex_index)


def test_list_sessions_includes_both_providers(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, tmp_path)
    sessions = history.list_sessions()
    by_provider = {s["provider"]: s for s in sessions}
    assert set(by_provider) == {"claude", "codex"}
    assert by_provider["claude"]["title"] == "hello world"
    assert by_provider["claude"]["message_count"] == 3
    assert by_provider["codex"]["title"] == "My Codex Thread"
    # developer-role messages are not counted as conversation.
    assert by_provider["codex"]["message_count"] == 2
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


async def test_dispatch_history_get_requires_params():
    conn = _new_conn()
    await conn._dispatch({"action": Action.HISTORY_GET})
    errors = _payloads(conn, Event.SESSION_ERROR)
    assert errors and "required" in errors[0]["message"]
