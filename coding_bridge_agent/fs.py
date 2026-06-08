"""Local filesystem browsing for the remote directory picker.

The browser (authenticated as the node owner) asks the node to list a directory
so the user can pick a working directory. Browsing stays within the OS
permissions of the account running the daemon; this is the same local trust
boundary as running a session.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Hard cap so a huge directory can't produce an unbounded payload.
MAX_ENTRIES = 1000


def list_dir(path: str | None = None, *, show_hidden: bool = False) -> dict[str, Any]:
    """List a directory's immediate children.

    Returns a dict with the resolved ``path``, its ``parent`` (or ``None`` at the
    root), and ``entries`` sorted directories-first then case-insensitively. On
    error a best-effort ``error`` string is included with an empty ``entries``.
    """
    try:
        base = Path(path).expanduser() if path else Path.home()
    except (RuntimeError, ValueError):
        base = Path.home()
    try:
        base = base.resolve()
    except OSError:
        base = base.absolute()

    if not base.exists():
        return _result(base, error="not found", entries=[])
    if not base.is_dir():
        base = base.parent

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(
            base.iterdir(),
            key=lambda p: (not _is_dir(p), p.name.lower()),
        )
    except PermissionError:
        return _result(base, error="permission denied", entries=[])
    except OSError as exc:
        return _result(base, error=str(exc), entries=[])

    for child in children:
        if not show_hidden and child.name.startswith("."):
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "dir" if _is_dir(child) else "file",
            }
        )

    return _result(base, entries=entries, truncated=truncated)


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _result(base: Path, *, entries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    parent = str(base.parent) if base.parent != base else None
    result: dict[str, Any] = {
        "path": str(base),
        "parent": parent,
        "sep": os.sep,
        "entries": entries,
    }
    result.update({k: v for k, v in extra.items() if v})
    return result
