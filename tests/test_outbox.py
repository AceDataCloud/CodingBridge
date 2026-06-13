"""Reliable delivery: the node outbox buffers durable browser events and resends
the unacked tail on reconnect, so nothing is lost across a relay drop.
"""
import json

from coding_bridge_agent import protocol
from coding_bridge_agent.config import Settings
from coding_bridge_agent.connection import BridgeConnection
from coding_bridge_agent.protocol import Event, event_payload


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _conn(outbox_max=5000):
    settings = Settings(bridge_url="https://bridge.test", outbox_max=outbox_max)
    conn = BridgeConnection(settings, "node_tok", provider_factory=lambda *a, **k: None)
    conn._ws = FakeWS()
    return conn


def _node_to_browser(ws):
    return [m for m in ws.sent if m.get("type") == protocol.NODE_TO_BROWSER]


async def test_durable_event_gets_node_seq_and_is_buffered():
    conn = _conn()
    await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text="hi"))
    sent = _node_to_browser(conn._ws)
    assert sent[0]["node_seq"] == 1
    assert sent[0]["payload"]["text"] == "hi"
    assert len(conn._outbox) == 1


async def test_node_seq_is_monotonic_across_sessions():
    conn = _conn()
    await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text="a"))
    await conn.send_payload(event_payload(Event.SESSION_TEXT, "s2", text="b"))
    assert [m["node_seq"] for m in _node_to_browser(conn._ws)] == [1, 2]


async def test_ephemeral_events_are_not_buffered_or_sequenced():
    conn = _conn()
    # No session_id at all.
    await conn.send_payload(event_payload(Event.CAPABILITIES, providers=[]))
    # Has a session_id but is a request-scoped response, not a live event.
    await conn.send_payload(event_payload(Event.HISTORY_DETAIL, "s1", events=[]))
    assert len(conn._outbox) == 0
    assert all("node_seq" not in m for m in _node_to_browser(conn._ws))


async def test_ack_trims_the_contiguous_prefix():
    conn = _conn()
    for i in range(3):
        await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text=str(i)))
    assert len(conn._outbox) == 3
    conn._ack_outbox(2)
    assert [e["node_seq"] for e in conn._outbox] == [3]
    conn._ack_outbox(3)
    assert len(conn._outbox) == 0


async def test_disconnect_buffers_then_reconnect_resends_tail():
    conn = _conn()
    conn._ws = None  # relay dropped mid-turn
    await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text="lost?"))
    await conn.send_payload(event_payload(Event.SESSION_RESULT, "s1"))
    # Nothing transmitted while offline, but both are retained for replay.
    assert len(conn._outbox) == 2

    fresh = FakeWS()
    conn._ws = fresh
    await conn._flush_outbox()
    resent = _node_to_browser(fresh)
    assert [m["payload"]["event"] for m in resent] == [
        Event.SESSION_TEXT,
        Event.SESSION_RESULT,
    ]
    # Same envelope ids as first emit → the relay dedups the resend.
    assert [m["node_seq"] for m in resent] == [1, 2]


async def test_node_ack_envelope_trims_outbox_via_on_raw():
    conn = _conn()
    for _ in range(3):
        await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text="x"))
    await conn._on_raw(
        json.dumps(protocol.envelope(protocol.NODE_ACK, {"up_to_node_seq": 2}))
    )
    assert [e["node_seq"] for e in conn._outbox] == [3]


async def test_overflow_drops_oldest_and_warns_on_flush():
    conn = _conn(outbox_max=2)
    for i in range(4):
        await conn.send_payload(event_payload(Event.SESSION_TEXT, "s1", text=str(i)))
    # Bounded: only the freshest 2 survive; the dropped span is flagged.
    assert len(conn._outbox) == 2
    assert "s1" in conn._truncated_sessions

    fresh = FakeWS()
    conn._ws = fresh
    await conn._flush_outbox()
    events = [m["payload"]["event"] for m in _node_to_browser(fresh)]
    # A truncation marker leads, then the retained tail.
    assert events[0] == Event.SESSION_STREAM_TRUNCATED
    assert _node_to_browser(fresh)[0]["payload"]["reason"] == "node_outbox_overflow"
    assert not conn._truncated_sessions  # cleared after warning
