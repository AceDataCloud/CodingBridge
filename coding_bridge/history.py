"""Read local Claude Code, Codex, and Copilot transcripts for history replay.

Transcripts live on disk as JSON lines:
  Claude Code: ``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``
  Codex:       ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl``
  Copilot:     ``$COPILOT_HOME/session-state/<session_id>/events.jsonl``

Both formats are normalised to the same inner event shapes a live session emits
(``prompt`` / ``text`` / ``thinking`` / ``tool_use`` / ``tool_result``) so the
browser renders history with its existing renderer. This module is read-only and
depends only on the standard library.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Module-level roots so tests can point them at fixtures via monkeypatch.
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CODEX_INDEX = Path.home() / ".codex" / "session_index.jsonl"
COPILOT_ROOT = (
    Path(os.environ.get("COPILOT_HOME") or str(Path.home() / ".copilot")) / "session-state"
)


def _vscode_chat_roots() -> list[Path]:
    """Candidate ``workspaceStorage`` dirs for VS Code Copilot Chat transcripts.

    Chat sessions live at ``<storage>/<workspace-hash>/chatSessions/<id>.jsonl``.
    ``VSCODE_CHAT_HOME`` overrides for tests / non-standard installs.
    """
    override = os.environ.get("VSCODE_CHAT_HOME")
    if override:
        return [Path(override)]
    home = Path.home()
    bases: list[Path | None] = [
        home / "Library" / "Application Support",  # macOS
        Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None,  # Windows
        home / ".config",  # Linux
    ]
    apps = ("Code", "Code - Insiders", "VSCodium", "Cursor")
    roots: list[Path] = []
    for base in bases:
        if base is None:
            continue
        roots.extend(base / app / "User" / "workspaceStorage" for app in apps)
    return roots


VSCODE_CHAT_ROOTS = _vscode_chat_roots()

_TITLE_MAX = 80
_DETAIL_MAX_EVENTS = 4000

_TAG_RE = re.compile(r"<[^>]+>")
_LEAD_TAG_RE = re.compile(r"^\s*<[^>]+>")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# --- public API ------------------------------------------------------------
def list_sessions(limit: int = 200) -> list[dict[str, Any]]:
    """Return session summaries from every provider, newest first."""
    limit = max(1, min(int(limit or 200), 1000))
    sessions = (
        _list_claude(limit)
        + _list_codex(limit)
        + _list_copilot(limit)
        + _list_vscode_chat(limit)
    )
    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return sessions[:limit]


def claude_known_ids(session_id: str) -> tuple[set[str], set[str]]:
    """Return ``(line_uuids, message_ids)`` already recorded in a claude transcript.

    Some claude CLI versions re-stream the whole resumed conversation before the
    new turn when launched with ``--resume``. Those replayed messages carry the
    transcript's original ids verbatim — the per-line ``uuid`` and the assistant
    ``message.id`` (``msg_…``). The resume turn matches against these sets to drop
    the replay and forward only genuinely new output. Empty when no transcript.
    """
    line_uuids: set[str] = set()
    msg_ids: set[str] = set()
    path = _claude_path(session_id)
    if path is None:
        return line_uuids, msg_ids
    for rec in _iter_jsonl(path):
        if rec.get("type") not in ("user", "assistant"):
            continue
        uid = rec.get("uuid")
        if isinstance(uid, str):
            line_uuids.add(uid)
        msg = rec.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("id"), str):
            msg_ids.add(msg["id"])
    return line_uuids, msg_ids


def claude_user_uuid_after(session_id: str, cut_uuid: str | None) -> str | None:
    """Return the uuid of the first user turn after ``cut_uuid`` in a transcript.

    Used as the ``rewind_files`` checkpoint id when an edit also restores code:
    we roll files back to the state captured before the edited turn ran, which
    is keyed by that turn's user-message uuid. ``cut_uuid=None`` means the very
    first user turn. Returns ``None`` when no transcript or no such turn.
    """
    path = _claude_path(session_id)
    if path is None:
        return None
    seen_cut = cut_uuid is None
    for rec in _iter_jsonl(path):
        uid = rec.get("uuid")
        if not seen_cut:
            if uid == cut_uuid:
                seen_cut = True
            continue
        if rec.get("type") == "user" and isinstance(uid, str):
            return uid
    return None


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
    elif provider == "copilot":
        # A "copilot" session may live in the CLI store or the VS Code Copilot
        # Chat store — both are Copilot to the user. Prefer the CLI store, fall
        # back to the VS Code panel transcript.
        path = _copilot_path(session_id)
        if path is not None:
            header, events = _copilot_read(path)
        else:
            vpath = _vscode_chat_path(session_id)
            if vpath is None:
                raise FileNotFoundError(f"copilot session not found: {session_id}")
            header, events = _vscode_chat_read(vpath)
    else:
        raise ValueError(f"unknown provider: {provider}")
    # Keep the MOST RECENT events when a transcript is huge — the tail is the
    # live context a user wants to continue, not the opening. `truncated` lets the
    # browser flag that earlier messages were dropped.
    truncated = len(events) > _DETAIL_MAX_EVENTS
    return {
        "provider": provider,
        "title": header.get("title") or "(no prompt)",
        "cwd": header.get("cwd"),
        "git_branch": header.get("git_branch"),
        "model": header.get("model"),
        "events": events[-_DETAIL_MAX_EVENTS:],
        "truncated": truncated,
    }


# Summarising a transcript reads the whole file, so listing many sessions is
# dominated by per-file IO. Fan the reads out across a small thread pool (the
# reads release the GIL); order and OSError-skipping match the old serial loop.
def _summaries(paths: list[Path], parse: Callable[[Path], dict[str, Any]]) -> list[dict[str, Any]]:
    if not paths:
        return []

    def _one(path: Path) -> dict[str, Any] | None:
        try:
            return parse(path)
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
        results = list(pool.map(_one, paths))
    return [r for r in results if r is not None]


# --- Claude Code -----------------------------------------------------------
def _list_claude(limit: int) -> list[dict[str, Any]]:
    if not CLAUDE_ROOT.exists():
        return []
    files = sorted(CLAUDE_ROOT.glob("*/*.jsonl"), key=_safe_mtime, reverse=True)[:limit]
    return _summaries(files, _claude_summary)


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
                cand = _first_text(msg.get("content"))
                if cand and not _is_context_noise(cand):
                    title = _clean_title(cand)
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
        if content.strip() and not _is_context_noise(content):
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
            if _is_context_noise(block["text"]):
                continue
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
    return _summaries(files, lambda path: _codex_summary(path, index))


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
                    cand = _unwrap_user_text(_codex_message_text(payload.get("content")))
                    if cand and not _is_context_noise(cand):
                        title = _clean_title(cand)
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
            text = _unwrap_user_text(text)
            if not text or _is_context_noise(text):
                return
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


def _list_copilot(limit: int) -> list[dict[str, Any]]:
    if not COPILOT_ROOT.exists():
        return []
    files = sorted(COPILOT_ROOT.glob("*/events.jsonl"), key=_safe_mtime, reverse=True)[:limit]
    return _summaries(files, _copilot_summary)


def _copilot_summary(path: Path) -> dict[str, Any]:
    meta = _copilot_workspace_meta(path.parent)
    title = ""
    count = 0
    cwd = meta.get("cwd")
    updated_at = _mtime_ms(path)
    for rec in _iter_jsonl(path):
        ts = _iso_ms(rec.get("timestamp"))
        if ts is not None:
            updated_at = ts
        etype = rec.get("type")
        data = rec.get("data")
        if not isinstance(data, dict):
            data = {}
        if etype == "session.start" and cwd is None:
            cwd = _copilot_cwd(data) or cwd
        elif etype == "user.message":
            text = _copilot_user_text(data)
            if text:
                count += 1
                if not title and not _is_context_noise(text):
                    title = _clean_title(text)
        elif etype == "assistant.message" and _copilot_assistant_text(data):
            count += 1
    return {
        "provider": "copilot",
        "session_id": path.parent.name,
        "title": meta.get("name") or title or "(no prompt)",
        "cwd": cwd,
        "git_branch": None,
        "updated_at": updated_at,
        "message_count": count,
    }


def _copilot_read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = _copilot_workspace_meta(path.parent)
    header: dict[str, Any] = {
        "cwd": meta.get("cwd"),
        "git_branch": None,
        "model": None,
        "title": meta.get("name") or "",
    }
    events: list[dict[str, Any]] = []
    for rec in _iter_jsonl(path):
        etype = rec.get("type")
        data = rec.get("data")
        if not isinstance(data, dict):
            data = {}
        ts = _iso_ms(rec.get("timestamp"))
        if etype == "session.start":
            header["cwd"] = _copilot_cwd(data) or header["cwd"]
        elif etype == "session.model_change":
            model = data.get("newModel")
            if isinstance(model, str) and model:
                header["model"] = model
        elif etype == "user.message":
            text = _copilot_user_text(data)
            if not text or _is_context_noise(text):
                continue
            events.append({"kind": "prompt", "text": text, "ts": ts})
            if not header["title"]:
                header["title"] = _clean_title(text)
        elif etype == "assistant.message":
            model = data.get("model")
            if header["model"] is None and isinstance(model, str) and model:
                header["model"] = model
            text = _copilot_assistant_text(data)
            if text:
                events.append({"kind": "text", "text": text, "ts": ts})
        elif etype == "tool.execution_start":
            events.append(
                {
                    "kind": "tool_use",
                    "tool": data.get("toolName"),
                    "tool_use_id": data.get("toolCallId"),
                    "input": data.get("arguments"),
                    "ts": ts,
                }
            )
        elif etype == "tool.execution_complete":
            events.append(
                {
                    "kind": "tool_result",
                    "tool_use_id": data.get("toolCallId"),
                    "content": _copilot_tool_result_text(data),
                    "is_error": not bool(data.get("success")),
                    "ts": ts,
                }
            )
    return header, events


def _copilot_path(session_id: str) -> Path | None:
    # session_id is joined straight into a path, so reject the traversal tokens
    # `_ID_RE` would otherwise allow (`.`/`..`); slashes are already disallowed.
    if not _safe_id(session_id) or session_id in (".", ".."):
        return None
    path = COPILOT_ROOT / session_id / "events.jsonl"
    return path if path.is_file() else None


def _copilot_workspace_meta(session_dir: Path) -> dict[str, Any]:
    meta = _simple_yaml(session_dir / "workspace.yaml")
    return {
        "cwd": meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
        "name": meta.get("name") if isinstance(meta.get("name"), str) else None,
    }


def _copilot_cwd(data: dict[str, Any]) -> str | None:
    context = data.get("context")
    if not isinstance(context, dict):
        return None
    cwd = context.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _copilot_user_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    transformed = data.get("transformedContent")
    if isinstance(transformed, str):
        # Copilot wraps the typed prompt with a leading <current_datetime> block
        # and a trailing <system_reminder>; strip those before generic unwrapping.
        transformed = _COPILOT_NOW_RE.sub("", transformed)
        transformed = _COPILOT_REMINDER_RE.sub("", transformed)
        return _unwrap_user_text(transformed).strip()
    return ""


def _copilot_assistant_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _copilot_tool_result_text(data: dict[str, Any]) -> Any:
    if data.get("success"):
        result = data.get("result")
        if isinstance(result, dict):
            if result.get("content") is not None:
                return _stringify(result.get("content"))
            if result.get("detailedContent") is not None:
                return _stringify(result.get("detailedContent"))
        return _stringify(result)
    error = data.get("error")
    if isinstance(error, dict) and error.get("message") is not None:
        return _stringify(error.get("message"))
    return _stringify(error)


# --- VS Code Copilot Chat --------------------------------------------------
# The panel stores each session as an append-only delta log of `{kind, v}` lines
# (format v3). We don't replay the deltas; we recursively find the request
# objects (dict with `message.text` + `response`), which is resilient to delta
# opcode changes across VS Code releases. These are surfaced as ordinary
# `copilot` sessions (same backend); read-only, so continuing makes a copy.
def _list_vscode_chat(limit: int) -> list[dict[str, Any]]:
    files: list[Path] = []
    for root in VSCODE_CHAT_ROOTS:
        if root.exists():
            files.extend(root.glob("*/chatSessions/*.jsonl"))
    files = sorted(files, key=_safe_mtime, reverse=True)[:limit]
    return _summaries(files, _vscode_chat_summary)


def _vscode_collect_requests(obj: Any, out: list[dict[str, Any]]) -> None:
    """Collect chat-request objects, pruning descent into the huge `response`."""
    if isinstance(obj, dict):
        msg = obj.get("message")
        if isinstance(msg, dict) and "text" in msg and "response" in obj:
            out.append(obj)
            return  # don't recurse into this request's response array
        for value in obj.values():
            _vscode_collect_requests(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _vscode_collect_requests(value, out)


def _vscode_requests(path: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for rec in _iter_jsonl(path):
        found: list[dict[str, Any]] = []
        _vscode_collect_requests(rec.get("v"), found)
        for req in found:
            rid = req.get("requestId")
            key = rid if isinstance(rid, str) else str(id(req))
            if key not in seen:
                seen[key] = req
                ordered.append(req)
    ordered.sort(key=lambda r: r.get("timestamp") or 0)
    return ordered


def _vscode_user_text(req: dict[str, Any]) -> str:
    msg = req.get("message")
    text = msg.get("text") if isinstance(msg, dict) else None
    return text.strip() if isinstance(text, str) else ""


def _vscode_chat_summary(path: Path) -> dict[str, Any]:
    reqs = _vscode_requests(path)
    title = ""
    updated_at = _mtime_ms(path)
    for req in reqs:
        ts = req.get("timestamp")
        if isinstance(ts, int):
            updated_at = max(updated_at, ts)
        if not title:
            text = _vscode_user_text(req)
            if text and not _is_context_noise(text):
                title = _clean_title(text)
    return {
        "provider": "copilot",
        "session_id": path.stem,
        "title": title or "(no prompt)",
        "cwd": _vscode_cwd(path),
        "git_branch": None,
        "updated_at": updated_at,
        "message_count": len(reqs),
    }


def _vscode_chat_read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] = {
        "cwd": _vscode_cwd(path),
        "git_branch": None,
        "model": None,
        "title": "",
    }
    events: list[dict[str, Any]] = []
    for req in _vscode_requests(path):
        ts = req.get("timestamp") if isinstance(req.get("timestamp"), int) else None
        model = req.get("modelId")
        if header["model"] is None and isinstance(model, str) and model:
            header["model"] = model
        text = _vscode_user_text(req)
        if text and not _is_context_noise(text):
            events.append({"kind": "prompt", "text": text, "ts": ts})
            if not header["title"]:
                header["title"] = _clean_title(text)
        _vscode_response_events(req.get("response"), ts, events)
    return header, events


def _vscode_response_events(response: Any, ts: int | None, events: list[dict[str, Any]]) -> None:
    if not isinstance(response, list):
        return
    for part in response:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        if kind is None:  # markdown text block: {"value": "...", ...}
            value = part.get("value")
            text = value.get("value") if isinstance(value, dict) else value
            if isinstance(text, str) and text.strip():
                events.append({"kind": "text", "text": text, "ts": ts})
        elif kind == "thinking":
            value = part.get("value")
            if isinstance(value, str) and value.strip():
                events.append({"kind": "thinking", "text": value, "ts": ts})
        elif kind == "toolInvocationSerialized":
            msg = part.get("pastTenseMessage") or part.get("invocationMessage")
            label = msg.get("value") if isinstance(msg, dict) else msg
            events.append(
                {
                    "kind": "tool_use",
                    "tool": part.get("toolId"),
                    "tool_use_id": part.get("toolCallId"),
                    "input": label if isinstance(label, str) else None,
                    "ts": ts,
                }
            )


def _vscode_chat_path(session_id: str) -> Path | None:
    if not _safe_id(session_id) or session_id in (".", ".."):
        return None
    matches: list[Path] = []
    for root in VSCODE_CHAT_ROOTS:
        if root.exists():
            matches.extend(root.glob(f"*/chatSessions/{session_id}.jsonl"))
    matches = [p for p in matches if p.is_file()]
    return max(matches, key=_safe_mtime) if matches else None


def _vscode_cwd(path: Path) -> str | None:
    # chatSessions/<id>.jsonl -> workspace.json sits two levels up (workspace hash dir)
    meta = path.parent.parent / "workspace.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    folder = data.get("folder") if isinstance(data, dict) else None
    if isinstance(folder, str) and folder.startswith("file://"):
        path = unquote(urlparse(folder).path)
        # Windows file URLs parse to "/C:/..." — drop the spurious leading slash.
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path or None
    return folder if isinstance(folder, str) and folder else None


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
        return "" if _is_context_noise(content) else content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                text = str(block["text"])
                if not _is_context_noise(text):
                    return text
    return ""


def _clean_title(text: str) -> str:
    if not text:
        return ""
    stripped = " ".join(_TAG_RE.sub(" ", text).split())
    return stripped[:_TITLE_MAX]


# Tool-injected context messages that are not real user prompts: Codex AGENTS.md
# / instruction / environment preambles, Copilot wrappers, and Claude IDE
# open-file reminders.
_NOISE_PREFIXES = (
    "# agents.md instructions",
    "# copilot instructions",
    "# context from my ide setup",
    "the following is the codex agent history",
    "the user opened the file",
    "the user selected the lines",
    "the user interrupted the previous turn",
    "caveat: the messages below",
    "<user_instructions>",
    "<environment_context>",
    "<cwd>",
    "<permissions",
    "<instructions>",
    "<ide_opened_file>",
    "<ide_selection>",
    "<system-reminder>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
)


def _is_context_noise(text: str) -> bool:
    """True if a user message is tool-injected context, not a typed prompt."""
    if not text:
        return True
    head = text.lstrip().lower()
    if head.startswith(_NOISE_PREFIXES):
        return True
    unwrapped = _LEAD_TAG_RE.sub("", head).lstrip()
    return bool(unwrapped) and unwrapped.startswith(_NOISE_PREFIXES)


_IDE_REQUEST_RE = re.compile(
    r"##\s*My request(?: for Codex)?:\s*\n?(.*)", re.IGNORECASE | re.DOTALL
)
_COPILOT_NOW_RE = re.compile(
    r"^\s*<current_datetime>.*?</current_datetime>\s*", re.IGNORECASE | re.DOTALL
)
_COPILOT_REMINDER_RE = re.compile(
    r"\s*<system_reminder>.*?</system_reminder>\s*$", re.IGNORECASE | re.DOTALL
)


def _unwrap_user_text(text: str) -> str:
    """Pull the typed prompt out of a Codex IDE-context wrapper; else return as-is."""
    if not text:
        return ""
    if text and text.lstrip().lower().startswith("# context from my ide setup"):
        match = _IDE_REQUEST_RE.search(text)
        return match.group(1).strip() if match else ""
    return text.strip()


def _simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                if value.lower() == "true":
                    data[key] = True
                elif value.lower() == "false":
                    data[key] = False
                elif value.lower() == "null":
                    data[key] = None
                elif re.fullmatch(r"-?\d+", value):
                    data[key] = int(value)
                else:
                    data[key] = value.strip("\"'")
    except (OSError, ValueError):
        return data
    return data


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
