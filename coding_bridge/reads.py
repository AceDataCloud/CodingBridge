"""Read watermarks for history sessions (``~/.ace-bridge/reads.json``).

The history drawer needs an "unread" signal that follows the user across
devices — read it on the phone, it must be read on the desktop too. Only the
node can compute that: it owns both the transcript mtime and whether the session
is still running. Keeping the watermark here rather than in the browser is what
makes it device-independent, since every browser asks the same machine.

A watermark, not a boolean: a session that was read and then runs again produces
new output and correctly goes back to unread. The watermark stored is the
``updated_at`` the browser actually rendered, not wall-clock "now" — output
appended after the render but before the mark arrives must stay unread, and a
transcript whose mtime is ahead of our clock must still be clearable.

Deliberately not reusing ``store`` here: it collapses "file missing" and "file
unreadable" into ``None``, which would make one transient read error reseed and
wipe every watermark.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
# Watermarks hold the session's own ``updated_at``, so evicting the smallest
# really does evict the stalest sessions — ones far past a listing's 1000-row cap.
_MAX_ENTRIES = 5000
# ``mark`` runs on a worker thread; without this two taps interleave their
# read-modify-write and one watermark is silently lost.
_LOCK = threading.Lock()


def _path(config_dir: Path | str) -> Path:
    return Path(config_dir).expanduser() / "reads.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _key(provider: Any, session_id: Any) -> str | None:
    """Watermark key. Providers can mint colliding ids, so identity is the pair."""
    if not isinstance(provider, str) or not _SAFE_ID.match(provider):
        return None
    if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
        return None
    return f"{provider}:{session_id}"


def _as_ms(value: Any) -> int:
    """Coerce untrusted JSON to a millisecond timestamp; anything odd becomes 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value) if value > 0 else 0


def _read(path: Path) -> dict[str, Any] | None:
    """Parsed watermarks, or ``None`` when there is genuinely nothing to read.

    Raises ``OSError`` when the file exists but can't be read — reseeding on a
    transient error (EMFILE, an AV lock, a denied ACL) would discard every
    watermark permanently.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both land here
        logger.warning("reads.json is corrupt, reseeding: %s", path)
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-write temp name: a fixed one lets two writers interleave into the
    # same file and rename the blend into place.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_or_seed(config_dir: Path | str) -> dict[str, Any]:
    """Return the watermark file, seeding a ``baseline`` on first use.

    Without that baseline the first run after an upgrade would flag every
    pre-existing transcript unread at once. The baseline stays a permanent floor:
    anything that last changed before this node started tracking counts as read.
    """
    path = _path(config_dir)
    data = _read(path)
    if isinstance(data, dict) and isinstance(data.get("reads"), dict):
        return data
    data = {"version": 1, "baseline": _now_ms(), "reads": {}}
    try:
        _write(path, data)
    except OSError as exc:
        logger.warning("could not seed %s: %s", path, exc)
    return data


def annotate(config_dir: Path | str, sessions: list[dict[str, Any]]) -> None:
    """Stamp ``unread`` on each summary; ``running`` must already be stamped."""
    try:
        data = _load_or_seed(config_dir)
        floor = _as_ms(data.get("baseline"))
        marks = data.get("reads") if isinstance(data.get("reads"), dict) else {}
    except Exception as exc:  # noqa: BLE001 - unread is cosmetic, never break the listing
        # Fail *closed*: an unreadable sidecar must not light up every row, which
        # is the exact flood the baseline exists to prevent.
        logger.warning("unread watermarks unavailable: %s", exc)
        for summary in sessions:
            summary["unread"] = False
        return
    for summary in sessions:
        key = _key(summary.get("provider"), summary.get("session_id"))
        seen = _as_ms(marks.get(key)) if key else 0
        # A session still executing a turn is not "something to catch up on" —
        # only finished work is worth a nudge.
        summary["unread"] = not summary.get("running") and _as_ms(
            summary.get("updated_at")
        ) > max(seen, floor)


def mark(
    config_dir: Path | str,
    provider: str,
    session_id: str,
    seen_updated_at: Any = None,
) -> int:
    """Record the session as read up to ``seen_updated_at``; returns the watermark.

    ``seen_updated_at`` is the ``updated_at`` the browser rendered (echoed back
    from a snapshot we sent it). Stamping that rather than "now" keeps output
    appended during the round trip unread, and lets a transcript whose mtime runs
    ahead of our clock still be cleared.
    """
    key = _key(provider, session_id)
    if key is None:
        raise ValueError(f"invalid provider/session_id: {provider!r}/{session_id!r}")
    watermark = _as_ms(seen_updated_at) or _now_ms()
    with _LOCK:
        data = _load_or_seed(config_dir)
        marks = data.get("reads")
        if not isinstance(marks, dict):
            marks = {}
            data["reads"] = marks
        # Never lower an existing watermark: a stale snapshot must not resurrect
        # a dot the user already cleared from another device.
        watermark = max(watermark, _as_ms(marks.get(key)))
        marks[key] = watermark
        _evict(marks)
        _write(_path(config_dir), data)
    return watermark


def _evict(marks: dict[str, Any]) -> None:
    overflow = len(marks) - _MAX_ENTRIES
    if overflow <= 0:
        return
    for key, _ in sorted(marks.items(), key=lambda kv: _as_ms(kv[1]))[:overflow]:
        marks.pop(key, None)
