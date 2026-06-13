import asyncio
import json

from coding_bridge_agent import protocol
from coding_bridge_agent.config import Settings
from coding_bridge_agent.connection import BridgeConnection
from coding_bridge_agent.protocol import Action, Event


class FakeProvider:
    name = "fake"

    def __init__(self, provider, session_id, emit, ask):
        self.provider = provider
        self.session_id = session_id
        self.emit = emit
        self.ask = ask
        self.prompts = []
        self.resume = None
        self.effort = None
        self.images = None
        self.attachments = None
        self.sent_model = None
        self.sent_effort = None
        self.sent_permission_mode = None
        self.closed = False

    async def start(
        self,
        prompt,
        *,
        cwd,
        model,
        permission_mode,
        effort=None,
        images=None,
        attachments=None,
        resume=None,
    ):
        self.prompts.append(prompt)
        self.resume = resume
        self.effort = effort
        self.images = images
        self.attachments = attachments

    async def send(
        self,
        prompt,
        *,
        images=None,
        attachments=None,
        model=None,
        effort=None,
        permission_mode=None,
    ):
        self.prompts.append(prompt)
        self.images = images
        self.attachments = attachments
        self.sent_model = model
        self.sent_effort = effort
        self.sent_permission_mode = permission_mode

    async def interrupt(self):
        pass

    async def aclose(self):
        self.closed = True


def fake_factory(provider, session_id, emit, ask):
    return FakeProvider(provider, session_id, emit, ask)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _new_conn():
    settings = Settings(bridge_url="https://bridge.test", permission_timeout=0.2)
    conn = BridgeConnection(settings, "node_tok", provider_factory=fake_factory)
    conn._ws = FakeWS()
    return conn


def _events(conn):
    return [
        msg["payload"].get("event")
        for msg in conn._ws.sent
        if msg.get("type") == protocol.NODE_TO_BROWSER
    ]


async def test_start_creates_session_and_emits_started():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "hello"})
    await asyncio.sleep(0.01)
    assert "s1" in conn.sessions
    assert Event.SESSION_STARTED in _events(conn)
    # Outgoing node→browser envelopes must carry from_node so the bridge can route.
    assert conn._ws.sent[0]["from_node"] == "node_tok"


async def test_ping_pongs():
    conn = _new_conn()
    await conn._dispatch({"action": Action.PING})
    assert {"event": "pong"} in [m["payload"] for m in conn._ws.sent]


async def test_permission_resolve_routes_to_session():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    session = conn.sessions["s1"]

    pending = asyncio.create_task(session._ask_permission("Bash", {"command": "ls"}, {}))
    await asyncio.sleep(0)

    requests = [
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.PERMISSION_REQUEST
    ]
    assert requests
    request_id = requests[0]["request_id"]

    conn._resolve_permission(
        {"action": Action.PERMISSION_RESOLVE, "request_id": request_id, "decision": "allow"}
    )
    assert await pending == "allow"


async def test_permissions_list_replays_pending_requests():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    session = conn.sessions["s1"]
    pending = asyncio.create_task(session._ask_permission("Bash", {"command": "ls"}, {}))
    await asyncio.sleep(0)
    conn._ws.sent.clear()

    # A reconnecting browser asks for outstanding prompts; the node re-emits them.
    await conn._dispatch({"action": Action.PERMISSIONS_LIST})
    snapshots = [
        m["payload"]
        for m in conn._ws.sent
        if m["payload"].get("event") == Event.PERMISSIONS_SNAPSHOT
    ]
    assert snapshots
    requests = snapshots[0]["requests"]
    assert len(requests) == 1
    assert requests[0]["tool"] == "Bash"
    assert requests[0]["session_id"] == "s1"

    # Resolving still works and clears the pending set.
    conn._resolve_permission({"request_id": requests[0]["request_id"], "decision": "allow"})
    assert await pending == "allow"
    assert session.pending_permissions() == []


async def test_close_removes_session():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    await asyncio.sleep(0.01)  # let the start turn finish before closing
    await conn._dispatch({"action": Action.SESSION_CLOSE, "session_id": "s1"})
    assert "s1" not in conn.sessions
    assert Event.SESSION_CLOSED in _events(conn)


async def test_unknown_action_reports_error():
    conn = _new_conn()
    await conn._dispatch({"action": "bogus.action", "session_id": "s1"})
    assert Event.SESSION_ERROR in _events(conn)


async def test_start_forwards_resume_session_id():
    conn = _new_conn()
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "s1",
            "prompt": "continue",
            "resume_session_id": "prev-session",
        }
    )
    await asyncio.sleep(0.01)
    assert conn.sessions["s1"]._provider.resume == "prev-session"


async def test_start_rejects_unsupported_provider():
    conn = _new_conn()
    await conn._dispatch(
        {"action": Action.SESSION_START, "session_id": "s1", "prompt": "x", "provider": "bogus"}
    )
    assert "s1" not in conn.sessions
    assert Event.SESSION_ERROR in _events(conn)


async def test_trace_id_propagates_to_session_events():
    conn = _new_conn()
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "s1",
            "prompt": "hello",
            "trace_id": "tr-abc",
        }
    )
    await asyncio.sleep(0.01)
    assert conn.sessions["s1"].trace_id == "tr-abc"
    # Every node→browser event for this turn must echo the trace id.
    traces = {
        m["payload"].get("trace_id")
        for m in conn._ws.sent
        if m.get("type") == protocol.NODE_TO_BROWSER
    }
    assert traces == {"tr-abc"}


async def test_follow_up_turn_updates_trace_id():
    conn = _new_conn()
    await conn._dispatch(
        {"action": Action.SESSION_START, "session_id": "s1", "prompt": "a", "trace_id": "tr-1"}
    )
    await asyncio.sleep(0.01)
    await conn._dispatch(
        {"action": Action.SESSION_SEND, "session_id": "s1", "prompt": "b", "trace_id": "tr-2"}
    )
    await asyncio.sleep(0.01)
    assert conn.sessions["s1"].trace_id == "tr-2"


async def test_send_log_envelope_uses_node_log_type():
    conn = _new_conn()
    await conn.send_log({"level": "info", "msg": "hi", "trace_id": "tr-1"})
    log_msgs = [m for m in conn._ws.sent if m.get("type") == protocol.NODE_LOG]
    assert len(log_msgs) == 1
    assert log_msgs[0]["from_node"] == "node_tok"
    assert log_msgs[0]["payload"]["trace_id"] == "tr-1"


async def test_start_accepts_codex_provider():
    conn = _new_conn()
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "s1",
            "prompt": "x",
            "provider": "codex",
            "effort": "high",
        }
    )
    await asyncio.sleep(0.01)
    assert "s1" in conn.sessions
    assert conn.sessions["s1"].provider == "codex"
    assert conn.sessions["s1"]._provider.effort == "high"


async def test_send_forwards_model_override_to_provider():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "hi"})
    await asyncio.sleep(0.01)
    await conn._dispatch(
        {
            "action": Action.SESSION_SEND,
            "session_id": "s1",
            "prompt": "switch",
            "model": "opus",
            "effort": "high",
            "permission_mode": "plan",
        }
    )
    await asyncio.sleep(0.01)
    provider = conn.sessions["s1"]._provider
    assert provider.sent_model == "opus"
    assert provider.sent_effort == "high"
    assert provider.sent_permission_mode == "plan"
    # The session remembers the new settings for later turns and snapshots.
    assert conn.sessions["s1"].model == "opus"
    assert conn.sessions["s1"].effort == "high"
    assert conn.sessions["s1"].permission_mode == "plan"


async def test_send_without_overrides_keeps_session_model():
    conn = _new_conn()
    await conn._dispatch(
        {"action": Action.SESSION_START, "session_id": "s1", "prompt": "hi", "model": "sonnet"}
    )
    await asyncio.sleep(0.01)
    await conn._dispatch({"action": Action.SESSION_SEND, "session_id": "s1", "prompt": "more"})
    await asyncio.sleep(0.01)
    # No model in the follow-up payload → the session keeps its current model and
    # still forwards it to the provider for the next turn.
    assert conn.sessions["s1"].model == "sonnet"
    assert conn.sessions["s1"]._provider.sent_model == "sonnet"



async def test_sessions_list_snapshot():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    conn._ws.sent.clear()
    await conn._dispatch({"action": Action.SESSIONS_LIST})
    snapshots = [
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.SESSIONS_SNAPSHOT
    ]
    assert snapshots
    assert snapshots[0]["sessions"][0]["session_id"] == "s1"


async def test_start_forwards_images_to_provider():
    conn = _new_conn()
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "s1",
            "prompt": "look",
            "images": ["data:image/png;base64,iVBORw0KGgo="],
        }
    )
    await asyncio.sleep(0.01)
    assert conn.sessions["s1"]._provider.images == ["data:image/png;base64,iVBORw0KGgo="]


async def test_start_forwards_attachments_to_provider():
    conn = _new_conn()
    attachments = [
        {
            "type": "file",
            "url": "https://cdn.acedata.cloud/report.pdf",
            "name": "report.pdf",
        }
    ]
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "s1",
            "prompt": "read",
            "attachments": attachments,
        }
    )
    await asyncio.sleep(0.01)
    assert conn.sessions["s1"]._provider.attachments == attachments


async def test_fs_list_returns_snapshot(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    conn = _new_conn()
    await conn._dispatch({"action": Action.FS_LIST, "path": str(tmp_path)})
    listings = [m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.FS_LIST]
    assert listings
    names = {e["name"] for e in listings[0]["entries"]}
    assert names == {"sub", "file.txt"}
    # Directories sort before files.
    assert listings[0]["entries"][0]["type"] == "dir"


async def test_capabilities_get_returns_descriptor():
    conn = _new_conn()
    await conn._dispatch({"action": Action.CAPABILITIES_GET})
    caps = [
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.CAPABILITIES
    ]
    assert caps
    names = [p["name"] for p in caps[0]["providers"]]
    assert names == ["claude", "codex"]

