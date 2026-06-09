"""WebSocket protocol shared with the coding-bridge relay.

The outer envelope and its ``type`` constants mirror coding-bridge's
``worker/app/protocol.py`` exactly — the bridge routes on ``type`` and forwards
``payload`` verbatim. The inner ``Action`` / ``Event`` sub-protocol is opaque to
the bridge and is carried inside ``payload`` between browser and node.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

PROTOCOL_VERSION = 1

# --- Outer envelope types (must match coding-bridge) -----------------------
BROWSER_TO_NODE = "browser.to_node"
BROWSER_LIST_NODES = "browser.list_nodes"
NODE_TO_BROWSER = "node.to_browser"
NODES_SNAPSHOT = "nodes.snapshot"
NODE_STATUS = "node.status"
ERROR = "error"
NODE_REGISTERED = "node.registered"
NODE_HEARTBEAT = "node.heartbeat"
NODE_HEARTBEAT_ACK = "node.heartbeat_ack"
# Node-originated structured log line. The node holds no CLS credentials, so the
# relay is its log sink: it ships these to Tencent CLS for end-to-end tracing.
NODE_LOG = "node.log"


def envelope(
    message_type: str, payload: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    """Build a protocol envelope with an id and millisecond timestamp."""
    message: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "ts": int(time.time() * 1000),
        "type": message_type,
        "payload": payload or {},
    }
    message.update(extra)
    return message


class Action:
    """Browser → node commands (inside ``payload``)."""

    SESSION_START = "session.start"
    SESSION_SEND = "session.send"
    SESSION_INTERRUPT = "session.interrupt"
    SESSION_CLOSE = "session.close"
    PERMISSION_RESOLVE = "permission.resolve"
    SESSIONS_LIST = "sessions.list"
    HISTORY_LIST = "history.list"
    HISTORY_GET = "history.get"
    FS_LIST = "fs.list"
    CAPABILITIES_GET = "capabilities.get"
    PING = "ping"


class Event:
    """Node → browser events (inside ``payload``)."""

    SESSION_STARTED = "session.started"
    SESSION_TEXT = "session.text"
    SESSION_THINKING = "session.thinking"
    SESSION_TOOL_USE = "session.tool_use"
    SESSION_TOOL_RESULT = "session.tool_result"
    PERMISSION_REQUEST = "permission.request"
    PERMISSION_RESOLVED = "permission.resolved"
    SESSION_RESULT = "session.result"
    SESSION_NOTICE = "session.notice"
    SESSION_ERROR = "session.error"
    SESSION_CLOSED = "session.closed"
    SESSIONS_SNAPSHOT = "sessions.snapshot"
    HISTORY_SNAPSHOT = "history.snapshot"
    HISTORY_DETAIL = "history.detail"
    FS_LIST = "fs.list"
    CAPABILITIES = "capabilities"
    PONG = "pong"


def event_payload(
    event: str,
    session_id: str | None = None,
    *,
    trace_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build an inner event payload for node → browser traffic.

    ``trace_id`` (when present) is echoed back so the browser can correlate this
    reply with the turn that produced it, and so CLS rows line up end to end.
    """
    payload: dict[str, Any] = {"event": event}
    if session_id is not None:
        payload["session_id"] = session_id
    if trace_id is not None:
        payload["trace_id"] = trace_id
    payload.update(fields)
    return payload
