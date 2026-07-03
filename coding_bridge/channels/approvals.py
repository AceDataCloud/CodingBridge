"""File-backed tool-approval store shared by the daemon and the portal.

``channels start`` (runs provider turns) and ``channels portal`` (the UI) are
separate processes, so a tool-permission request raised inside a channel turn is
published here as a small JSON file the portal can list; the portal writes a
decision file back and the daemon polls for it. Everything lives under
``<config_dir>/approvals/`` and never leaves the box — same local trust boundary
as the rest of the agent.

Opt-in: nothing writes here unless an instance sets ``require_approval = true``,
so the default daemon path is completely unchanged.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

# Request ids are provider-generated uuids; validate before using one as a
# filename so a hostile id can't escape the approvals directory.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# A pending request older than this is considered abandoned (the turn that
# raised it has long since timed out) and is neither surfaced nor honored.
_DEFAULT_TTL = 600.0


def _write_atomic(path: Path, body: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".appr-", suffix=".tmp")
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


class ApprovalStore:
    """Cross-process pending/decision registry for channel tool approvals."""

    def __init__(self, root: Path | str, *, ttl: float = _DEFAULT_TTL) -> None:
        self._root = Path(root)
        self._ttl = ttl

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(self._root, 0o700)

    @staticmethod
    def valid_id(request_id: str) -> bool:
        return bool(isinstance(request_id, str) and _ID_RE.match(request_id))

    def _req_path(self, rid: str) -> Path:
        return self._root / f"{rid}.request.json"

    def _dec_path(self, rid: str) -> Path:
        return self._root / f"{rid}.decision.json"

    def create(self, request_id: str, descriptor: dict[str, Any]) -> None:
        """Publish a pending request. Descriptor should carry only display fields."""
        if not self.valid_id(request_id):
            raise ValueError("invalid request id")
        self._ensure_root()
        payload = {"id": request_id, "created_at": time.time(), **descriptor}
        _write_atomic(self._req_path(request_id), json.dumps(payload))

    def list_pending(self) -> list[dict[str, Any]]:
        """Every still-undecided, non-stale pending request (oldest first)."""
        out: list[dict[str, Any]] = []
        if not self._root.exists():
            return out
        now = time.time()
        for p in self._root.glob("*.request.json"):
            rid = p.name[: -len(".request.json")]
            if self._dec_path(rid).exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if now - float(data.get("created_at", 0) or 0) > self._ttl:
                continue
            out.append(data)
        out.sort(key=lambda d: d.get("created_at", 0))
        return out

    def decide(self, request_id: str, decision: str) -> bool:
        """Record a decision for a pending request. Returns False if unknown."""
        if not self.valid_id(request_id) or decision not in ("allow", "deny"):
            return False
        if not self._req_path(request_id).exists():
            return False
        _write_atomic(
            self._dec_path(request_id),
            json.dumps({"decision": decision, "decided_at": time.time()}),
        )
        return True

    def poll_decision(self, request_id: str) -> str | None:
        """Return ``allow`` / ``deny`` once decided, else ``None``."""
        if not self.valid_id(request_id):
            return None
        try:
            data = json.loads(self._dec_path(request_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        decision = data.get("decision")
        return decision if decision in ("allow", "deny") else None

    def cleanup(self, request_id: str) -> None:
        """Remove the request + decision files (best-effort)."""
        if not self.valid_id(request_id):
            return
        for p in (self._req_path(request_id), self._dec_path(request_id)):
            with contextlib.suppress(OSError):
                p.unlink()


__all__ = ["ApprovalStore"]
