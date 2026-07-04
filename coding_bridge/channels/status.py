"""Run-status heartbeat + recent-turn ring for the channels daemon.

``channels start`` (the daemon) and ``channels portal`` (the UI) are separate
processes. The daemon writes a small **heartbeat** file describing what's
running and appends a bounded ring of recent turn *metrics* (adapter, instance,
provider, outcome, latency, sizes) — **never** message content. The portal reads
both to render a live status panel. Everything lives under
``<config_dir>/status/`` and never leaves the box — same local trust boundary as
the rest of the agent.

Opt-in by process: only ``channels start`` writes here; if the daemon isn't
running the files simply go stale / absent and the portal shows "stopped".
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_RUN_FILE = "run.json"
_TURNS_FILE = "turns.jsonl"
_MAX_TURNS = 50

#: A heartbeat older than this (seconds) is treated as "not running" — covers a
#: daemon that was hard-killed (SIGKILL / power loss) without clearing run.json.
RUN_STALE_SECONDS = 60.0


def _write_atomic(path: Path, body: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".status-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class StatusStore:
    """Cross-process run heartbeat + bounded recent-turn ring."""

    def __init__(self, root: Path | str, *, max_turns: int = _MAX_TURNS) -> None:
        self._root = Path(root)
        self._max_turns = max(1, int(max_turns))

    @property
    def root(self) -> Path:
        return self._root

    def _ensure(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(self._root, 0o700)

    # ---- run heartbeat (daemon writes, portal reads) -------------------- #

    def write_run(self, channels: list[dict[str, Any]], *, started_at: float) -> None:
        """Refresh the heartbeat. ``channels`` is a content-free descriptor list."""
        self._ensure()
        payload = {
            "pid": os.getpid(),
            "started_at": float(started_at),
            "updated_at": time.time(),
            "channels": list(channels),
        }
        with contextlib.suppress(OSError):
            _write_atomic(self._root / _RUN_FILE, json.dumps(payload))

    def clear_run(self) -> None:
        """Remove the heartbeat on a clean shutdown (best-effort)."""
        with contextlib.suppress(OSError):
            (self._root / _RUN_FILE).unlink()

    def read_run(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self._root / _RUN_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def is_running(self, *, now: float | None = None) -> bool:
        run = self.read_run()
        if not isinstance(run, dict):
            return False
        updated = run.get("updated_at")
        if not isinstance(updated, (int, float)):
            return False
        return (now or time.time()) - float(updated) < RUN_STALE_SECONDS

    # ---- recent-turn ring (daemon appends, portal reads) --------------- #

    def record_turn(self, event: dict[str, Any]) -> None:
        """Append one content-free turn metric, keeping only the last N."""
        self._ensure()
        path = self._root / _TURNS_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        except OSError:
            lines = []
        lines.append(json.dumps(event))
        lines = lines[-self._max_turns :]
        with contextlib.suppress(OSError):
            _write_atomic(path, "\n".join(lines) + "\n")

    def read_turns(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent turns, newest first."""
        try:
            lines = (self._root / _TURNS_FILE).read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            return []
        out: list[dict[str, Any]] = []
        for ln in lines[-max(0, limit) :]:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        out.reverse()
        return out


__all__ = ["StatusStore", "RUN_STALE_SECONDS"]
