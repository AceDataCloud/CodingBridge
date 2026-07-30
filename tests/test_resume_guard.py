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

    def __init__(self, content: list, uuid: str | None, message_id: str | None = None):
        self.content = content
        self.uuid = uuid
        if message_id is not None:
            self.message_id = message_id


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


async def test_replayed_message_without_uuid_is_dropped_by_message_id():
    """A replay whose uuid is absent must still be caught by its message id.

    Observed in production: a whole transcript arrived as complete messages with
    no matching uuid, so it was forwarded as live output AND — because it looked
    genuine — the replay's result ended the turn, so the real answer never came.
    """
    provider, events = _capturing()
    messages = [
        _Assistant([_TextBlock("old one")], uuid=None, message_id="msg_OLD1"),
        _Assistant(
            [_ToolUseBlock("Bash", "t_old", {"command": "ls"})],
            uuid="U_UNSEEN",
            message_id="msg_OLD2",
        ),
        _Result(),  # replay's result — must NOT end the turn
        _Assistant([_TextBlock("PROBE_OK")], uuid="U_NEW", message_id="msg_NEW"),
        _Result(),
    ]
    await _run(provider, messages, uuids=set(), msg_ids={"msg_OLD1", "msg_OLD2"})

    assert [e["text"] for e in events if e["event"] == "session.text"] == ["PROBE_OK"]
    assert not [e for e in events if e["event"] == "session.tool_use"]
    assert len([e for e in events if e["event"] == "session.result"]) == 1


async def test_guard_rearms_on_every_turn(monkeypatch, tmp_path):
    """The guard must protect the 2nd turn too, not just the first resumed one.

    A session stays registered across turns, so a CLI respawned mid-conversation
    replays into a later turn — which the old first-turn-only guard let through.
    """
    from coding_bridge import history

    transcript = tmp_path / "proj" / "sdk-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"assistant","uuid":"U_OLD","message":{"id":"msg_OLD"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    provider, events = _capturing()
    provider._sdk_session_id = "sdk-1"
    provider._client = _FakeClient(
        [
            _Assistant([_TextBlock("old")], uuid="U_OLD"),
            _Result(),
            _Assistant([_TextBlock("PROBE_OK")], uuid="U_NEW"),
            _Result(),
        ]
    )

    async def query(_prompt):
        return None

    provider._client.query = query
    # A follow-up turn: no explicit resume id is passed, so the guard has to
    # derive it from the session it is already attached to.
    await provider._turn("follow-up")

    assert [e["text"] for e in events if e["event"] == "session.text"] == ["PROBE_OK"]
    assert len([e for e in events if e["event"] == "session.result"]) == 1


async def test_watermark_ignores_lines_written_after_the_turn_started(tmp_path, monkeypatch):
    """Ids appended after the watermark are this turn's, so they must pass."""
    from coding_bridge import history

    transcript = tmp_path / "proj" / "sdk-2.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"assistant","uuid":"U_OLD","message":{"id":"msg_OLD"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    watermark = history.claude_watermark("sdk-2")
    assert watermark == 1

    # The turn runs and the CLI appends its output.
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","uuid":"U_NEW","message":{"id":"msg_NEW"}}\n')

    uuids, msg_ids = history.claude_ids_before("sdk-2", watermark)
    assert uuids == {"U_OLD"}
    assert msg_ids == {"msg_OLD"}
