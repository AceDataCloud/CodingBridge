import asyncio
import json

from coding_bridge_agent import protocol
from coding_bridge_agent.config import Settings
from coding_bridge_agent.connection import BridgeConnection
from coding_bridge_agent.protocol import Action, Event


class FakeProvider:
    name = "fake"

    def __init__(self, session_id, emit, ask):
        self.session_id = session_id
        self.emit = emit
        self.ask = ask
        self.prompts = []
        self.closed = False

    async def start(self, prompt, *, cwd, model, permission_mode):
        self.prompts.append(prompt)

    async def send(self, prompt):
        self.prompts.append(prompt)

    async def interrupt(self):
        pass

    async def aclose(self):
        self.closed = True


def fake_factory(session_id, emit, ask):
    return FakeProvider(session_id, emit, ask)


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
        m["payload"]
        for m in conn._ws.sent
        if m["payload"].get("event") == Event.PERMISSION_REQUEST
    ]
    assert requests
    request_id = requests[0]["request_id"]

    conn._resolve_permission(
        {"action": Action.PERMISSION_RESOLVE, "request_id": request_id, "decision": "allow"}
    )
    assert await pending == "allow"


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


async def test_sessions_list_snapshot():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    conn._ws.sent.clear()
    await conn._dispatch({"action": Action.SESSIONS_LIST})
    snapshots = [
        m["payload"]
        for m in conn._ws.sent
        if m["payload"].get("event") == Event.SESSIONS_SNAPSHOT
    ]
    assert snapshots
    assert snapshots[0]["sessions"][0]["session_id"] == "s1"
