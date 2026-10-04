"""Claude partial messages and immediate Codex complete-message delivery."""

from coding_bridge.config import Settings
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


# --- Codex complete messages ------------------------------------------------


async def test_codex_forwards_complete_text_once_without_artificial_deltas():
    provider, events = _capturing(CodexProvider)
    text = "Hello from Codex" * 1000
    await provider._handle_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )
    assert len(events) == 1
    assert events[0]["event"] == "session.text"
    assert events[0]["text"] == text


async def test_claude_partial_block_repaired_by_complete_message():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_start(0))
    await provider._handle_message(_delta(0, "partial"))
    await provider._handle_message(_stop(0))
    await provider._handle_message(_Assistant([_TextBlock("complete answer")]))
    texts = [e for e in events if e['event'] == 'session.text']
    assert texts[-1]['text'] == 'complete answer'
    assert texts[0]['id'] == texts[-1]['id']
    # A later unstreamed answer is not swallowed because an earlier block streamed.
    await provider._handle_message(_Assistant([_TextBlock("final answer")]))
    assert events[-1]['text'] == 'final answer'


async def test_claude_stream_start_with_initial_text_is_preserved():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    await provider._handle_message(_Stream({
        'type': 'content_block_start', 'index': 0,
        'content_block': {'type': 'text', 'text': 'initial'},
    }))
    await provider._handle_message(_stop(0))
    await provider._handle_message(_Assistant([_TextBlock('initial')]))
    assert [e['text'] for e in events if e['event'] == 'session.text'] == ['initial']


async def test_claude_complete_before_stop_keeps_order_without_duplicates():
    provider, events = _capturing(ClaudeProvider)
    provider._begin_stream_turn()
    for text in ['first answer', 'second answer', 'final answer']:
        await provider._handle_message(_Stream({'type': 'message_start', 'message': {'id': text}}))
        await provider._handle_message(_start(0))
        await provider._handle_message(_delta(0, text[:3]))
        await provider._handle_message(_Assistant([_TextBlock(text)]))
        await provider._handle_message(_stop(0))
    texts = [e for e in events if e['event'] == 'session.text']
    assert [e['text'] for e in texts] == ['first answer', 'second answer', 'final answer']
    assert len({e['id'] for e in texts}) == 3


async def test_claude_tool_images_do_not_forward_base64_payloads():
    from types import SimpleNamespace
    provider, events = _capturing(ClaudeProvider)
    block = SimpleNamespace(tool_use_id='read-image', is_error=False, content=[
        {'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': 'binary-secret',
        }},
        {'type': 'text', 'text': 'image metadata'},
    ])
    await provider._handle_block(block)
    assert 'binary-secret' not in events[-1]['content']
    assert 'image/png' in events[-1]['content']
    assert 'image metadata' in events[-1]['content']
