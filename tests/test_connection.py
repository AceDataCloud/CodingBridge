import asyncio
import json
from types import SimpleNamespace

from coding_bridge import protocol
from coding_bridge.config import Settings
from coding_bridge.connection import BridgeConnection
from coding_bridge.protocol import Action, Event


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
    assert (await pending).decision == "allow"


async def test_permission_resolve_forwards_ask_user_question_answer():
    conn = _new_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "s1", "prompt": "x"})
    session = conn.sessions["s1"]

    pending = asyncio.create_task(
        session._ask_permission("AskUserQuestion", {"questions": [{"question": "Q1"}]}, {})
    )
    await asyncio.sleep(0)
    request_id = next(
        m["payload"]["request_id"]
        for m in conn._ws.sent
        if m["payload"].get("event") == Event.PERMISSION_REQUEST
    )

    answer = {"answers": {"Q1": "A"}}
    conn._resolve_permission(
        {"request_id": request_id, "decision": "allow", "answer": answer}
    )
    resolution = await pending
    assert resolution.decision == "allow"
    assert resolution.answer == answer


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
    assert (await pending).decision == "allow"
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
    assert names == ["claude", "codex", "copilot"]


# --- Session identity (re-key on real id, reattach on resume) ----------------
class IdentifyingProvider(FakeProvider):
    """A provider that announces its real (SDK) id on the first turn, like Claude."""

    real_id = "real-sdk-id"

    async def start(self, prompt, **kwargs):
        await super().start(prompt, **kwargs)
        await self.emit(
            protocol.event_payload(
                Event.SESSION_IDENTIFIED, self.session_id, sdk_session_id=self.real_id
            )
        )


def _identifying_conn():
    settings = Settings(bridge_url="https://bridge.test", permission_timeout=0.2)
    conn = BridgeConnection(
        settings, "node_tok", provider_factory=lambda p, s, e, a: IdentifyingProvider(p, s, e, a)
    )
    conn._ws = FakeWS()
    return conn


async def test_start_rekeys_session_to_real_id():
    conn = _identifying_conn()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "prov-1", "prompt": "hi"})
    await asyncio.sleep(0.01)
    # Registry is keyed by the real id; the provisional id resolves via alias.
    assert "real-sdk-id" in conn.sessions
    assert "prov-1" not in conn.sessions
    assert conn._session("prov-1") is conn.sessions["real-sdk-id"]
    assert conn._session("real-sdk-id") is conn.sessions["real-sdk-id"]
    # The browser is told to re-key.
    assert Event.SESSION_IDENTIFIED in _events(conn)


async def test_resume_reattaches_to_live_session():
    conn = _identifying_conn()
    await conn._dispatch(
        {"action": Action.SESSION_START, "session_id": "prov-1", "prompt": "first"}
    )
    await asyncio.sleep(0.01)
    session = conn.sessions["real-sdk-id"]
    # Resuming from history addresses the real id: continue the live session
    # rather than spawning a second client over the same transcript.
    await conn._dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "real-sdk-id",
            "prompt": "again",
            "resume_session_id": "real-sdk-id",
        }
    )
    await asyncio.sleep(0.01)
    assert len(conn.sessions) == 1
    assert conn.sessions["real-sdk-id"] is session
    assert session._provider.prompts == ["first", "again"]


async def test_history_list_does_not_flag_idle_in_memory_session(monkeypatch):
    """A completed session stays in the registry (reattachable) but is idle, so it
    must NOT be flagged running — otherwise every in-memory session shows a live
    dot in the drawer."""
    from coding_bridge import history

    conn = _identifying_conn()  # IdentifyingProvider finishes its turn immediately
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "prov-1", "prompt": "hi"})
    await asyncio.sleep(0.01)
    assert conn.sessions["real-sdk-id"].status == "idle"
    monkeypatch.setattr(
        history,
        "list_sessions",
        lambda limit=200: [{"provider": "claude", "session_id": "real-sdk-id"}],
    )
    await conn._dispatch({"action": Action.HISTORY_LIST})
    snapshot = next(
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.HISTORY_SNAPSHOT
    )
    assert snapshot["sessions"][0]["running"] is False


async def test_history_list_flags_only_actively_running_session(monkeypatch):
    """Only a session executing a turn right now is flagged running."""
    from coding_bridge import history

    gate = asyncio.Event()

    class HangingProvider(FakeProvider):
        async def start(self, prompt, **kwargs):
            await super().start(prompt, **kwargs)
            await gate.wait()  # hold the turn open so status stays "running"

    settings = Settings(bridge_url="https://bridge.test", permission_timeout=0.2)
    conn = BridgeConnection(
        settings, "node_tok", provider_factory=lambda p, s, e, a: HangingProvider(p, s, e, a)
    )
    conn._ws = FakeWS()
    await conn._dispatch({"action": Action.SESSION_START, "session_id": "live-1", "prompt": "go"})
    await asyncio.sleep(0.01)
    assert conn.sessions["live-1"].status == "running"
    monkeypatch.setattr(
        history,
        "list_sessions",
        lambda limit=200: [
            {"provider": "claude", "session_id": "live-1"},
            {"provider": "claude", "session_id": "other"},
        ],
    )
    await conn._dispatch({"action": Action.HISTORY_LIST})
    snapshot = next(
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.HISTORY_SNAPSHOT
    )
    by_id = {s["session_id"]: s["running"] for s in snapshot["sessions"]}
    assert by_id == {"live-1": True, "other": False}
    gate.set()  # let the hung turn finish so the test tears down cleanly
    await asyncio.sleep(0.01)


# --- Unread watermark --------------------------------------------------------
def _reads_conn(tmp_path):
    settings = Settings(
        bridge_url="https://bridge.test", permission_timeout=0.2, config_dir=tmp_path
    )
    conn = BridgeConnection(settings, "node_tok", provider_factory=fake_factory)
    conn._ws = FakeWS()
    return conn


def _snapshot(conn):
    return next(
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.HISTORY_SNAPSHOT
    )


def _stub_history(monkeypatch, updated_at, seen=None):
    from coding_bridge import history

    if seen is not None:
        seen.clear()

    def _list(limit=200):
        if seen is not None:
            seen.append(limit)
        return [{"provider": "claude", "session_id": "s1", "updated_at": updated_at}]

    monkeypatch.setattr(history, "list_sessions", _list)


async def test_history_list_flags_unread_after_baseline(tmp_path, monkeypatch):
    from coding_bridge import reads

    conn = _reads_conn(tmp_path)
    monkeypatch.setattr(reads, "_now_ms", lambda: 1_000_000)
    # Finished after the baseline seeded on first use → genuinely new to the user.
    _stub_history(monkeypatch, 1_500_000)
    await conn._dispatch({"action": Action.HISTORY_LIST})
    assert _snapshot(conn)["sessions"][0]["unread"] is True


async def test_mark_read_clears_unread_and_resends_snapshot(tmp_path, monkeypatch):
    import json

    from coding_bridge import reads

    conn = _reads_conn(tmp_path)
    monkeypatch.setattr(reads, "_now_ms", lambda: 2_000_000)
    _stub_history(monkeypatch, 1_500_000)
    await conn._dispatch(
        {
            "action": Action.HISTORY_MARK_READ,
            "provider": "claude",
            "session_id": "s1",
            "updated_at": 1_500_000,
        }
    )
    # Marking replies with a refreshed listing, so the browser needs no new branch.
    assert _snapshot(conn)["sessions"][0]["unread"] is False
    marks = json.loads((tmp_path / "reads.json").read_text())["reads"]
    assert marks == {"claude:s1": 1_500_000}


async def test_mark_read_forwards_the_drawer_limit(tmp_path, monkeypatch):
    """A paginated drawer must not shrink to the default after one tap."""
    conn = _reads_conn(tmp_path)
    limits: list[int] = []
    _stub_history(monkeypatch, 1_500_000, seen=limits)
    await conn._dispatch(
        {
            "action": Action.HISTORY_MARK_READ,
            "provider": "claude",
            "session_id": "s1",
            "limit": 1000,
        }
    )
    assert limits == [1000]


async def test_mark_read_falls_back_to_the_transcripts_own_timestamp(tmp_path, monkeypatch):
    """No client updated_at → use the listing's, so a future mtime is still clearable."""
    import json

    from coding_bridge import reads

    conn = _reads_conn(tmp_path)
    monkeypatch.setattr(reads, "_now_ms", lambda: 1_000_000)
    future = 9_000_000  # a restored backup / skewed clock leaves mtime ahead of us
    _stub_history(monkeypatch, future)
    await conn._dispatch(
        {"action": Action.HISTORY_MARK_READ, "provider": "claude", "session_id": "s1"}
    )
    assert _snapshot(conn)["sessions"][0]["unread"] is False
    assert json.loads((tmp_path / "reads.json").read_text())["reads"]["claude:s1"] == future


async def test_running_flag_does_not_cross_providers(tmp_path, monkeypatch):
    """A live codex session must not mark a same-id claude transcript running."""
    from coding_bridge import history

    conn = _reads_conn(tmp_path)
    conn.sessions["dup"] = SimpleNamespace(status="running", provider="codex")
    monkeypatch.setattr(
        history,
        "list_sessions",
        lambda limit=200: [
            {"provider": "claude", "session_id": "dup", "updated_at": 1},
            {"provider": "codex", "session_id": "dup", "updated_at": 1},
        ],
    )
    await conn._dispatch({"action": Action.HISTORY_LIST})
    flags = {s["provider"]: s["running"] for s in _snapshot(conn)["sessions"]}
    assert flags == {"claude": False, "codex": True}


async def test_mark_read_failure_still_answers_with_a_snapshot(tmp_path, monkeypatch):
    """A cosmetic failure must not emit a durable, forever-replayed session error."""
    conn = _reads_conn(tmp_path)
    _stub_history(monkeypatch, 1_500_000)
    # No provider → the watermark write raises; the listing must still come back.
    await conn._dispatch({"action": Action.HISTORY_MARK_READ, "session_id": "s1"})
    assert Event.SESSION_ERROR not in _events(conn)
    assert _snapshot(conn)["sessions"][0]["session_id"] == "s1"
    assert len(conn._outbox) == 0


async def test_history_list_survives_a_broken_watermark_file(tmp_path, monkeypatch):
    """A cosmetic sidecar must never turn the listing into an error."""
    from coding_bridge import reads

    conn = _reads_conn(tmp_path)
    _stub_history(monkeypatch, 1_500_000)

    def boom(*args, **kwargs):
        raise RuntimeError("sidecar exploded")

    monkeypatch.setattr(reads, "annotate", boom)
    await conn._dispatch({"action": Action.HISTORY_LIST})
    assert _snapshot(conn)["sessions"][0]["session_id"] == "s1"
    assert Event.SESSION_ERROR not in _events(conn)



def _detail(conn):
    return next(
        m["payload"] for m in conn._ws.sent if m["payload"].get("event") == Event.HISTORY_DETAIL
    )


async def _detail_for(conn, monkeypatch, transcript_model, sidecar=None):
    """Run history.get with stubbed transcript + sidecar, return the reply."""
    from coding_bridge import history, store

    monkeypatch.setattr(
        history,
        "read_session",
        lambda provider, sid: {"provider": provider, "model": transcript_model, "events": []},
    )
    if sidecar:
        store.save(conn.settings.config_dir / "sessions" / "s1.json", sidecar)
    await conn._dispatch(
        {"action": Action.HISTORY_GET, "provider": "claude", "session_id": "s1"}
    )
    return _detail(conn)


async def test_history_detail_separates_selector_from_resolved_model(tmp_path, monkeypatch):
    conn = _reads_conn(tmp_path)
    detail = await _detail_for(
        conn,
        monkeypatch,
        "claude-opus-5",
        {"version": 2, "provider": "claude", "model": "opus[1m]"},
    )
    assert detail["model"] == "opus[1m]"
    assert detail["resolved_model"] == "claude-opus-5"


async def test_history_detail_does_not_resume_from_polluted_legacy_model(tmp_path, monkeypatch):
    conn = _reads_conn(tmp_path)
    detail = await _detail_for(
        conn,
        monkeypatch,
        "claude-opus-5",
        {"provider": "claude", "model": "claude-opus-5"},
    )
    assert "model" not in detail
    assert detail["resolved_model"] == "claude-opus-5"


async def test_history_detail_roundtrips_explicit_bare_selector(tmp_path, monkeypatch):
    conn = _reads_conn(tmp_path)
    detail = await _detail_for(
        conn,
        monkeypatch,
        "claude-opus-5",
        {"version": 2, "provider": "claude", "model": "opus"},
    )
    assert detail["model"] == "opus"
    assert detail["resolved_model"] == "claude-opus-5"
