"""Read local Claude Code and Codex transcripts for history replay.

Transcripts live on disk as JSON lines:
  Claude Code: ``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``
  Codex:       ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl``

Both formats are normalised to the same inner event shapes a live session emits
(``prompt`` / ``text`` / ``thinking`` / ``tool_use`` / ``tool_result``) so the
browser renders history with its existing renderer. This module is read-only and
depends only on the standard library.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

# Module-level roots so tests can point them at fixtures via monkeypatch.
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CODEX_INDEX = Path.home() / ".codex" / "session_index.jsonl"

_TITLE_MAX = 80
_DETAIL_MAX_EVENTS = 4000

_TAG_RE = re.compile(r"<[^>]+>")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# --- public API ------------------------------------------------------------
def list_sessions(limit: int = 200) -> list[dict[str, Any]]:
    """Return session summaries from both providers, newest first."""
    limit = max(1, min(int(limit or 200), 1000))
    sessions = _list_claude(limit) + _list_codex(limit)
    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return sessions[:limit]


def read_session(provider: str, session_id: str) -> dict[str, Any]:
    """Return a normalised transcript ``{provider,title,cwd,model,...,events}``."""
    if provider == "claude":
        path = _claude_path(session_id)
        if path is None:
            raise FileNotFoundError(f"claude session not found: {session_id}")
        header, events = _claude_read(path)
    elif provider == "codex":
        path = _codex_path(session_id)
        if path is None:
            raise FileNotFoundError(f"codex session not found: {session_id}")
        header, events = _codex_read(path, _codex_index())
    else:
        raise ValueError(f"unknown provider: {provider}")
    return {
        "provider": provider,
        "title": header.get("title") or "(no prompt)",
        "cwd": header.get("cwd"),
        "git_branch": header.get("git_branch"),
        "model": header.get("model"),
        "events": events[:_DETAIL_MAX_EVENTS],
    }


# --- Claude Code -----------------------------------------------------------
def _list_claude(limit: int) -> list[dict[str, Any]]:
    if not CLAUDE_ROOT.exists():
        return []
    files = sorted(CLAUDE_ROOT.glob("*/*.jsonl"), key=_safe_mtime, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            out.append(_claude_summary(path))
        except OSError:
            continue
    return out


def _claude_summary(path: Path) -> dict[str, Any]:
    cwd: str | None = None
    git: str | None = None
    title = ""
    count = 0
    for rec in _iter_jsonl(path):
        if cwd is None and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if git is None and isinstance(rec.get("gitBranch"), str):
            git = rec["gitBranch"]
        kind = rec.get("type")
        msg = rec.get("message")
        if kind in ("user", "assistant") and isinstance(msg, dict):
            count += 1
            if not title and kind == "user":
                title = _clean_title(_first_text(msg.get("content")))
    return {
        "provider": "claude",
        "session_id": path.stem,
        "title": title or "(no prompt)",
        "cwd": cwd,
        "git_branch": git,
        "updated_at": _mtime_ms(path),
        "message_count": count,
    }


def _claude_read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] = {"cwd": None, "git_branch": None, "model": None, "title": ""}
    events: list[dict[str, Any]] = []
    for rec in _iter_jsonl(path):
        ts = _iso_ms(rec.get("timestamp"))
        if header["cwd"] is None and isinstance(rec.get("cwd"), str):
            header["cwd"] = rec["cwd"]
        if header["git_branch"] is None and isinstance(rec.get("gitBranch"), str):
            header["git_branch"] = rec["gitBranch"]
        kind = rec.get("type")
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        if header["model"] is None and isinstance(msg.get("model"), str):
            header["model"] = msg["model"]
        content = msg.get("content")
        if kind == "user":
            _claude_user_events(content, ts, events, header)
        elif kind == "assistant":
            _claude_assistant_events(content, ts, events)
    return header, events


def _claude_user_events(
    content: Any, ts: int | None, events: list[dict[str, Any]], header: dict[str, Any]
) -> None:
    if isinstance(content, str):
        if content.strip():
            events.append({"kind": "prompt", "text": content, "ts": ts})
            if not header["title"]:
                header["title"] = _clean_title(content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            events.append({"kind": "prompt", "text": block["text"], "ts": ts})
            if not header["title"]:
                header["title"] = _clean_title(block["text"])
        elif btype == "tool_result":
            events.append(
                {
                    "kind": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": _stringify(block.get("content")),
                    "is_error": bool(block.get("is_error")),
                    "ts": ts,
                }
            )


def _claude_assistant_events(content: Any, ts: int | None, events: list[dict[str, Any]]) -> None:
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            events.append({"kind": "thinking", "text": block.get("thinking", ""), "ts": ts})
        elif btype == "text":
            events.append({"kind": "text", "text": block.get("text", ""), "ts": ts})
        elif btype == "tool_use":
            events.append(
                {
                    "kind": "tool_use",
                    "tool": block.get("name"),
                    "tool_use_id": block.get("id"),
                    "input": block.get("input"),
                    "ts": ts,
                }
            )
        elif btype == "tool_result":
            events.append(
                {
                    "kind": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": _stringify(block.get("content")),
                    "is_error": bool(block.get("is_error")),
                    "ts": ts,
                }
            )


def _claude_path(session_id: str) -> Path | None:
    if not _safe_id(session_id):
        return None
    matches = sorted(CLAUDE_ROOT.glob(f"*/{session_id}.jsonl"), key=_safe_mtime, reverse=True)
    return matches[0] if matches else None


# --- Codex -----------------------------------------------------------------
def _list_codex(limit: int) -> list[dict[str, Any]]:
    if not CODEX_ROOT.exists():
        return []
    files = sorted(CODEX_ROOT.glob("**/rollout-*.jsonl"), key=_safe_mtime, reverse=True)[:limit]
    index = _codex_index()
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            out.append(_codex_summary(path, index))
        except OSError:
            continue
    return out


def _codex_summary(path: Path, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sid: str | None = None
    cwd: str | None = None
    git: str | None = None
    title = ""
    count = 0
    for rec in _iter_jsonl(path):
        ptype = rec.get("type")
        payload = rec.get("payload") or {}
        if ptype == "session_meta":
            sid = payload.get("id")
            cwd = payload.get("cwd")
            git = _codex_git(payload)
        elif ptype == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                count += 1
                if not title and role == "user":
                    title = _clean_title(_codex_message_text(payload.get("content")))
    sid = sid or _codex_sid_from_name(path)
    meta = index.get(sid or "", {})
    return {
        "provider": "codex",
        "session_id": sid,
        "title": meta.get("thread_name") or title or "(no prompt)",
        "cwd": cwd,
        "git_branch": git,
        "updated_at": _iso_ms(meta.get("updated_at")) or _mtime_ms(path),
        "message_count": count,
    }


def _codex_read(
    path: Path, index: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] = {"cwd": None, "git_branch": None, "model": None, "title": ""}
    events: list[dict[str, Any]] = []
    sid: str | None = None
    for rec in _iter_jsonl(path):
        ptype = rec.get("type")
        payload = rec.get("payload") or {}
        ts = _iso_ms(rec.get("timestamp"))
        if ptype == "session_meta":
            sid = payload.get("id")
            header["cwd"] = payload.get("cwd")
            header["git_branch"] = _codex_git(payload)
            header["model"] = payload.get("model") or payload.get("model_provider")
            continue
        if ptype == "turn_context" and not header["model"]:
            header["model"] = payload.get("model")
            continue
        if ptype != "response_item":
            continue
        _codex_response_event(payload, ts, events, header)
    if not header["title"]:
        meta = index.get(sid or _codex_sid_from_name(path), {})
        header["title"] = meta.get("thread_name") or ""
    return header, events


def _codex_response_event(
    payload: dict[str, Any], ts: int | None, events: list[dict[str, Any]], header: dict[str, Any]
) -> None:
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return
        text = _codex_message_text(payload.get("content"))
        if not text.strip():
            return
        if role == "user":
            events.append({"kind": "prompt", "text": text, "ts": ts})
            if not header["title"]:
                header["title"] = _clean_title(text)
        else:
            events.append({"kind": "text", "text": text, "ts": ts})
    elif ptype == "reasoning":
        text = _codex_reasoning_text(payload.get("summary"))
        if text:
            events.append({"kind": "thinking", "text": text, "ts": ts})
    elif ptype == "function_call":
        events.append(
            {
                "kind": "tool_use",
                "tool": payload.get("name"),
                "tool_use_id": payload.get("call_id"),
                "input": _maybe_json(payload.get("arguments")),
                "ts": ts,
            }
        )
    elif ptype == "function_call_output":
        events.append(
            {
                "kind": "tool_result",
                "tool_use_id": payload.get("call_id"),
                "content": _stringify(payload.get("output")),
                "is_error": False,
                "ts": ts,
            }
        )
    elif ptype == "web_search_call":
        events.append(
            {
                "kind": "tool_use",
                "tool": "web_search",
                "tool_use_id": payload.get("id"),
                "input": payload.get("action"),
                "ts": ts,
            }
        )


def _codex_path(session_id: str) -> Path | None:
    if not _safe_id(session_id):
        return None
    matches = sorted(
        CODEX_ROOT.glob(f"**/rollout-*-{session_id}.jsonl"), key=_safe_mtime, reverse=True
    )
    return matches[0] if matches else None


def _codex_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not CODEX_INDEX.exists():
        return index
    for rec in _iter_jsonl(CODEX_INDEX):
        sid = rec.get("id")
        if isinstance(sid, str):
            index[sid] = {
                "thread_name": rec.get("thread_name"),
                "updated_at": rec.get("updated_at"),
            }
    return index


def _codex_git(payload: dict[str, Any]) -> str | None:
    git = payload.get("git")
    if isinstance(git, dict):
        branch = git.get("branch") or git.get("commit")
        return branch if isinstance(branch, str) else None
    return git if isinstance(git, str) else None


def _codex_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts)


def _codex_reasoning_text(summary: Any) -> str:
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    parts = [b["text"] for b in summary if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts)


def _codex_sid_from_name(path: Path) -> str:
    match = _UUID_RE.search(path.name)
    return match.group(0) if match else path.stem


# --- shared helpers --------------------------------------------------------
def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return str(block["text"])
    return ""


def _clean_title(text: str) -> str:
    if not text:
        return ""
    stripped = " ".join(_TAG_RE.sub(" ", text).split())
    return stripped[:_TITLE_MAX]


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _stringify(content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _iso_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _safe_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(_ID_RE.match(session_id))


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_ms(path: Path) -> int:
    return int(_safe_mtime(path) * 1000)
