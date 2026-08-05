"""Non-destructive compatibility repair for Claude Code transcripts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from . import history

_REPAIR_VERSION = "unsigned-thinking-v1"
_REPAIR_NAMESPACE = uuid.UUID("ae5aa1df-998a-48a8-8c1f-c449262e573e")


class TranscriptRecoveryError(RuntimeError):
    """A transcript could not be repaired without risking conversation loss."""


def prepare_resume(session_id: str) -> str:
    """Return a resumable session id, repairing unsigned thinking in a fork."""
    source = history.claude_path(session_id)
    if source is None:
        return session_id

    try:
        raw = source.read_bytes()
        records = _parse_records(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptRecoveryError(f"cannot read Claude transcript {session_id}") from exc

    repaired, changed = _repair_records(records)
    if not changed:
        return session_id

    digest = hashlib.sha256(raw).hexdigest()
    repaired_id = str(uuid.uuid5(_REPAIR_NAMESPACE, f"{_REPAIR_VERSION}:{session_id}:{digest}"))
    output = _encode_records(repaired, repaired_id)
    target = source.with_name(f"{repaired_id}.jsonl")
    _atomic_write(target, output)
    return repaired_id


def _parse_records(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TranscriptRecoveryError("Claude transcript contains a non-object record")
        records.append(record)
    return records


def _repair_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = [block for block in content if not _unsigned_thinking(block)]
        if len(kept) == len(content):
            continue
        if not kept:
            raise TranscriptRecoveryError(
                "unsigned thinking is the assistant message's only content"
            )
        message["content"] = kept
        changed = True
    return records, changed


def _unsigned_thinking(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") != "thinking":
        return False
    signature = block.get("signature")
    return not isinstance(signature, str) or not signature.strip()


def _encode_records(records: list[dict[str, Any]], session_id: str) -> bytes:
    lines: list[str] = []
    for record in records:
        if "sessionId" in record:
            record["sessionId"] = session_id
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


def _atomic_write(target: Path, content: bytes) -> None:
    try:
        if target.read_bytes() == content:
            return
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise TranscriptRecoveryError(f"cannot inspect repaired transcript {target.name}") from exc

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, target)
    except OSError as exc:
        raise TranscriptRecoveryError(f"cannot write repaired transcript {target.name}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
