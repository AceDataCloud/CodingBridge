from coding_bridge import protocol
from coding_bridge.protocol import Event, envelope, event_payload


def test_envelope_shape():
    env = envelope(protocol.NODE_HEARTBEAT, {"capabilities": ["claude"]}, from_node="n1")
    assert env["v"] == protocol.PROTOCOL_VERSION
    assert env["type"] == "node.heartbeat"
    assert env["payload"] == {"capabilities": ["claude"]}
    assert env["from_node"] == "n1"
    assert "id" in env and "ts" in env


def test_envelope_default_payload():
    env = envelope(protocol.NODE_TO_BROWSER)
    assert env["payload"] == {}


def test_event_payload_with_session():
    payload = event_payload(Event.SESSION_TEXT, "s1", text="hi")
    assert payload == {"event": "session.text", "session_id": "s1", "text": "hi"}


def test_event_payload_without_session():
    assert event_payload(Event.PONG) == {"event": "pong"}


def test_event_payload_carries_trace_id():
    payload = event_payload(Event.SESSION_TEXT, "s1", trace_id="tr-1", text="hi")
    assert payload == {
        "event": "session.text",
        "session_id": "s1",
        "trace_id": "tr-1",
        "text": "hi",
    }


def test_event_payload_omits_trace_id_when_absent():
    assert "trace_id" not in event_payload(Event.SESSION_TEXT, "s1", text="hi")


def test_type_constants_match_bridge():
    # These strings are the contract with coding-bridge; do not drift.
    assert protocol.BROWSER_TO_NODE == "browser.to_node"
    assert protocol.NODE_TO_BROWSER == "node.to_browser"
    assert protocol.NODE_HEARTBEAT == "node.heartbeat"
    assert protocol.NODE_HEARTBEAT_ACK == "node.heartbeat_ack"
    assert protocol.NODE_REGISTERED == "node.registered"
    assert protocol.NODE_LOG == "node.log"
