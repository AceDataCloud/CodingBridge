"""Resume-replay guard: a resumed first turn must forward only new output.

Some claude CLI versions re-stream the whole resumed transcript (ending in its
own result) before the new turn. Those replayed messages reuse the transcript's
original ids; the guard drops them and forwards only the genuine turn.
"""

from coding_bridge.config import Settings
from coding_bridge.providers.claude import ClaudeProvider


def _capturing():
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(*_args):
        return "deny"

    return ClaudeProvider("s1", emit, ask, Settings()), events


class _Stream:
    def __init__(self, event: dict):
        self.event = event


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    def __init__(self, name, tid, inp):
        self.name = name
        self.id = tid
        self.input = inp


class _Assistant:
    """Complete AssistantMessage carrying the transcript line ``uuid``."""

    def __init__(self, content: list, uuid: str | None):
        self.content = content
        self.uuid = uuid


class _Result:
    subtype = "success"
    is_error = False
    result = None
    total_cost_usd = 0.0


class _Init:
    subtype = "init"

    def __init__(self, version: str):
        self.data = {"claude_code_version": version}


def _msg_start(mid: str) -> _Stream:
    return _Stream({"type": "message_start", "message": {"id": mid}})


def _start(index: int, btype: str = "text") -> _Stream:
    return _Stream(
        {"type": "content_block_start", "index": index, "content_block": {"type": btype}}
    )


def _delta(index: int, text: str) -> _Stream:
    return _Stream(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def _stop(index: int) -> _Stream:
    return _Stream({"type": "content_block_stop", "index": index})


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def receive_messages(self):
        for message in self._messages:
            yield message


async def _run(provider, messages, *, uuids, msg_ids):
    provider._client = _FakeClient(messages)
    provider._begin_stream_turn()
    provider._gate_active = True
    provider._gate_uuids = set(uuids)
    provider._gate_msg_ids = set(msg_ids)
    await provider._gated_receive()


async def test_gate_drops_replayed_transcript_keeps_new_turn():
    provider, events = _capturing()
    messages = [
        _Init("1.0.120"),
        # --- replayed transcript (original ids) ---
        _msg_start("msg_OLD"),
        _start(0),
        _delta(0, "old assistant text"),
        _stop(0),
        _Assistant([_TextBlock("old assistant text")], uuid="U_OLD_A"),
        _Assistant([_ToolUseBlock("Bash", "t_old", {"command": "ls"})], uuid="U_OLD_T"),
        _Result(),  # replay's terminating result — must be swallowed
        # --- genuine new turn (fresh ids) ---
        _msg_start("msg_NEW"),
        _start(1),
        _delta(1, "PROBE_OK"),
        _stop(1),
        _Assistant([_TextBlock("PROBE_OK")], uuid="U_NEW"),
        _Result(),
    ]
    await _run(provider, messages, uuids={"U_OLD_A", "U_OLD_T"}, msg_ids={"msg_OLD"})

    deltas = [e["text"] for e in events if e["event"] == "session.text_delta"]
    assert deltas == ["PROBE_OK"]  # no replayed delta leaked

    texts = [e["text"] for e in events if e["event"] == "session.text"]
    assert texts == ["PROBE_OK"]  # exactly the new turn's text, committed once

    assert not [e for e in events if e["event"] == "session.tool_use"]  # replayed tool dropped

    results = [e for e in events if e["event"] == "session.result"]
    assert len(results) == 1  # only the genuine turn's result ends the turn


async def test_gate_is_noop_when_no_replay():
    """A clean CLI (no replayed ids) must pass the new turn through unchanged."""
    provider, events = _capturing()
    messages = [
        _Init("2.1.168"),
        _msg_start("msg_NEW"),
        _start(0),
        _delta(0, "Hello"),
        _stop(0),
        _Assistant([_TextBlock("Hello")], uuid="U_NEW"),
        _Result(),
    ]
    # Transcript ids exist but none of them appear in this turn.
    await _run(provider, messages, uuids={"U_GONE"}, msg_ids={"msg_GONE"})

    assert [e["text"] for e in events if e["event"] == "session.text_delta"] == ["Hello"]
    assert [e["text"] for e in events if e["event"] == "session.text"] == ["Hello"]
    assert len([e for e in events if e["event"] == "session.result"]) == 1


async def test_gate_swallows_only_one_replay_result():
    """An empty genuine turn (result with no content) must still terminate."""
    provider, events = _capturing()
    messages = [
        _Assistant([_TextBlock("old")], uuid="U_OLD"),
        _Result(),  # replay result — swallowed
        _Result(),  # genuine (empty) turn result — must end the turn
    ]
    await _run(provider, messages, uuids={"U_OLD"}, msg_ids=set())
    assert len([e for e in events if e["event"] == "session.result"]) == 1
