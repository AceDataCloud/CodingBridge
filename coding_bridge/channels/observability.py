"""Structured, privacy-safe observability for channel turns.

Every inbound message that the dispatcher actually runs produces exactly one
``turn`` event via :func:`log_turn`. The event carries stable, greppable fields
(instance, provider, outcome, latency, sizes) but **never** the message text or
the reply body — this daemon runs on an end user's machine, so message content
must not leak into logs (local file, stderr, or the optional relay/CLS sink).

The event is emitted on the ``coding-bridge.channels`` logger as a normal INFO
record with the fields attached via ``extra=``, so:

* the local file / stderr sinks render a readable one-liner, and
* :class:`coding_bridge.logs.BridgeLogForwarder` (when connected) ships the same
  fields upstream as a ``node.log`` envelope for correlation in CLS — with no
  extra wiring here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("coding-bridge.channels")

# The mutually exclusive ways a turn can end. Stable string codes so dashboards
# / log queries can group on them.
TurnOutcome = Literal["ok", "provider_error", "timeout", "empty", "send_failed"]


@dataclass(frozen=True)
class TurnEvent:
    """One completed dispatcher turn, reduced to non-sensitive metrics."""

    adapter: str
    instance_id: str
    provider: str
    outcome: TurnOutcome
    latency_ms: int
    prompt_chars: int
    reply_chars: int
    # Opaque per-turn id (the dispatcher's session id) so a log line can be
    # tied back to a provider session without exposing content.
    session_id: str


def log_turn(event: TurnEvent) -> None:
    """Emit exactly one structured INFO record for a completed turn.

    Content-free by construction: only sizes and codes, never the text. The
    ``extra`` dict is what the relay forwarder reads (``trace_id``/``session_id``
    are already understood by :class:`BridgeLogForwarder`); the rest ride along
    for local rendering and future CLS field extraction.
    """
    logger.info(
        "channel turn: adapter=%s instance=%s provider=%s outcome=%s "
        "latency_ms=%d prompt_chars=%d reply_chars=%d",
        event.adapter,
        event.instance_id,
        event.provider,
        event.outcome,
        event.latency_ms,
        event.prompt_chars,
        event.reply_chars,
        extra={
            "session_id": event.session_id,
            "channel_instance": event.instance_id,
            "channel_provider": event.provider,
            "channel_outcome": event.outcome,
            "channel_latency_ms": event.latency_ms,
            "channel_prompt_chars": event.prompt_chars,
            "channel_reply_chars": event.reply_chars,
        },
    )
