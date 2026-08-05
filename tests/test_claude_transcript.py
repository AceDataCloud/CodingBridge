"""Compatibility repair for Claude transcripts with unsigned thinking blocks."""

import json
import uuid

import pytest

from coding_bridge import claude_transcript, history


def _write(path, records):
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assistant(session_id, content):
    return {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": "assistant-1",
        "parentUuid": "user-1",
        "message": {"role": "assistant", "content": content},
    }


def test_repair_forks_and_removes_only_unsigned_thinking(monkeypatch, tmp_path):
    session_id = "11111111-1111-1111-1111-111111111111"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    records = [
        {"type": "user", "sessionId": session_id, "uuid": "user-1", "message": "go"},
        _assistant(
            session_id,
            [
                {"type": "thinking", "thinking": "unsigned"},
                {"type": "thinking", "thinking": "signed", "signature": "opaque"},
                {"type": "text", "text": "answer"},
                {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}},
            ],
        ),
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": "result-1",
            "parentUuid": "assistant-1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
            },
        },
        {"type": "file-history-snapshot", "messageId": "assistant-1", "snapshot": {}},
    ]
    _write(transcript, records)
    original = transcript.read_bytes()
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    repaired_id = claude_transcript.prepare_resume(session_id)

    assert repaired_id != session_id
    uuid.UUID(repaired_id)
    assert transcript.read_bytes() == original
    repaired = _records(transcript.with_name(f"{repaired_id}.jsonl"))
    content = repaired[1]["message"]["content"]
    assert content == records[1]["message"]["content"][1:]
    assert repaired[2]["message"]["content"][0]["tool_use_id"] == "tool-1"
    assert repaired[3] == records[3]
    assert {record["sessionId"] for record in repaired if "sessionId" in record} == {
        repaired_id
    }


def test_healthy_transcript_is_unchanged(monkeypatch, tmp_path):
    session_id = "22222222-2222-2222-2222-222222222222"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    _write(
        transcript,
        [_assistant(session_id, [{"type": "thinking", "thinking": "ok", "signature": "sig"}])],
    )
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    assert claude_transcript.prepare_resume(session_id) == session_id
    assert list(transcript.parent.glob("*.jsonl")) == [transcript]


def test_repair_is_idempotent_for_same_source(monkeypatch, tmp_path):
    session_id = "33333333-3333-3333-3333-333333333333"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    _write(
        transcript,
        [
            _assistant(
                session_id,
                [{"type": "thinking", "thinking": "bad"}, {"type": "text", "text": "ok"}],
            )
        ],
    )
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    first = claude_transcript.prepare_resume(session_id)
    second = claude_transcript.prepare_resume(session_id)

    assert first == second
    assert len(list(transcript.parent.glob("*.jsonl"))) == 2


@pytest.mark.parametrize("signature", [None, "", "   ", 123])
def test_invalid_signature_values_are_removed(monkeypatch, tmp_path, signature):
    session_id = "44444444-4444-4444-4444-444444444444"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    thinking = {"type": "thinking", "thinking": "bad"}
    if signature is not None:
        thinking["signature"] = signature
    _write(transcript, [_assistant(session_id, [thinking, {"type": "text", "text": "kept"}])])
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    repaired_id = claude_transcript.prepare_resume(session_id)
    content = _records(transcript.with_name(f"{repaired_id}.jsonl"))[0]["message"]["content"]
    assert content == [{"type": "text", "text": "kept"}]


def test_malformed_transcript_leaves_no_repair(monkeypatch, tmp_path):
    session_id = "55555555-5555-5555-5555-555555555555"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"assistant"}\nnot-json\n', encoding="utf-8")
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    with pytest.raises(claude_transcript.TranscriptRecoveryError):
        claude_transcript.prepare_resume(session_id)
    assert list(transcript.parent.glob("*.jsonl")) == [transcript]


def test_thinking_only_message_fails_instead_of_breaking_parent_chain(monkeypatch, tmp_path):
    session_id = "66666666-6666-6666-6666-666666666666"
    transcript = tmp_path / "project" / f"{session_id}.jsonl"
    _write(transcript, [_assistant(session_id, [{"type": "thinking", "thinking": "bad"}])])
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)

    with pytest.raises(claude_transcript.TranscriptRecoveryError):
        claude_transcript.prepare_resume(session_id)
    assert list(transcript.parent.glob("*.jsonl")) == [transcript]


def test_missing_transcript_keeps_requested_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "CLAUDE_ROOT", tmp_path)
    assert claude_transcript.prepare_resume("not-present") == "not-present"
