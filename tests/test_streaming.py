"""Token-level streaming: Claude partial messages and codex typewriter parity."""

from coding_bridge.config import Settings
from coding_bridge.providers import codex
from coding_bridge.providers.claude import ClaudeProvider
from coding_bridge.providers.codex import CodexProvider


def _capturing(cls):
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(*_args):
        return "deny"

    return cls("s1", emit, ask, Settings()), events


class _Stream:
    """Stand-in for the SDK ``StreamEvent`` (carries a raw ``event`` dict)."""

    def __init__(self, event: dict):
        self.event = event


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ThinkingBlock:
    def __init__(self, thinking: str):
        self.thinking = thinking


class _Assistant:
    def __init__(self, content: list):
        self.content = content


class _Result:
    subtype = "success"
    is_error = False


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


# --- Claude partial-message streaming --------------------------------------


async def test_claude_streams_text_deltas_then_commits():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_start(0))
    await provider._handle_message(_delta(0, "Hel"))
    await provider._handle_message(_delta(0, "lo"))
    await provider._handle_message(_stop(0))
    # The assembled AssistantMessage text must not be re-emitted.
    await provider._handle_message(_Assistant([_TextBlock("Hello")]))

    deltas = [e for e in events if e["event"] == "session.text_delta"]
    assert [d["text"] for d in deltas] == ["Hel", "lo"]
    assert len({d["id"] for d in deltas}) == 1

    texts = [e for e in events if e["event"] == "session.text"]
    assert len(texts) == 1  # only the stop-driven commit, no duplicate block text
    assert texts[0]["text"] == "Hello"
    assert texts[0]["id"] == deltas[0]["id"]


async def test_claude_without_stream_events_emits_block_text():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_Assistant([_TextBlock("Plain")]))
    texts = [e for e in events if e["event"] == "session.text"]
    assert [t["text"] for t in texts] == ["Plain"]
    assert all("id" not in t for t in texts)  # no streaming id when not streamed


async def test_claude_flush_commits_unstopped_text_before_result():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_start(0))
    await provider._handle_message(_delta(0, "abc"))
    # No content_block_stop; the ResultMessage ends the turn.
    await provider._handle_message(_Result())

    kinds = [e["event"] for e in events]
    assert "session.text" in kinds and "session.result" in kinds
    assert kinds.index("session.text") < kinds.index("session.result")
    text = next(e for e in events if e["event"] == "session.text")
    assert text["text"] == "abc"


async def test_claude_thinking_is_not_streamed():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_start(0, "thinking"))
    await provider._handle_message(
        _Stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hmm"},
            }
        )
    )
    assert events == []  # thinking never streams as text deltas
    await provider._handle_message(_Assistant([_ThinkingBlock("hmm done")]))
    assert events[-1]["event"] == "session.thinking"
    assert events[-1]["text"] == "hmm done"


async def test_claude_two_text_blocks_get_distinct_ids():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_start(0))
    await provider._handle_message(_delta(0, "first"))
    await provider._handle_message(_stop(0))
    await provider._handle_message(_start(1))
    await provider._handle_message(_delta(1, "second"))
    await provider._handle_message(_stop(1))
    ids = {e["id"] for e in events if e["event"] == "session.text_delta"}
    assert len(ids) == 2


# --- Codex typewriter parity ------------------------------------------------


async def test_codex_agent_message_streams_then_commits():
    provider, events = _capturing(CodexProvider)
    await provider._handle_event(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Hello, world! Codex here."},
        }
    )
    deltas = [e for e in events if e["event"] == "session.text_delta"]
    assert deltas
    assert "".join(d["text"] for d in deltas) == "Hello, world! Codex here."
    assert len({d["id"] for d in deltas}) == 1

    texts = [e for e in events if e["event"] == "session.text"]
    assert len(texts) == 1
    assert texts[0]["text"] == "Hello, world! Codex here."
    assert texts[0]["id"] == deltas[0]["id"]
    assert events[-1]["event"] == "session.text"  # commit is last


async def test_codex_stream_chunk_count_is_capped():
    provider, events = _capturing(CodexProvider)
    text = "x" * 5000
    await provider._handle_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )
    deltas = [e for e in events if e["event"] == "session.text_delta"]
    assert len(deltas) <= codex.STREAM_CHUNK_TARGET
    assert "".join(d["text"] for d in deltas) == text


async def test_codex_short_message_still_streams():
    provider, events = _capturing(CodexProvider)
    await provider._handle_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}
    )
    deltas = [e for e in events if e["event"] == "session.text_delta"]
    assert "".join(d["text"] for d in deltas) == "ok"
    assert events[-1]["event"] == "session.text"
    assert events[-1]["text"] == "ok"
