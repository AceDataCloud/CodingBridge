"""Per-session settings sidecar (``~/.ace-bridge/sessions/<sid>.json``).

Claude/Codex transcripts record ``cwd`` and ``model`` but **never** the reasoning
effort or the permission ("edit") mode — those are runtime parameters, not
conversation content. Without them a resumed-from-history session can't recover
what tier/mode it last ran with and resets to defaults. We persist the last-used
settings keyed by the on-disk (SDK) transcript session id, so a history replay can
restore exactly what the session was driven with — on any device that asks the node.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import store

# In schema v2, `model` has one meaning: the exact launch selection. Transcript
# model IDs are exposed separately as `resolved_model` and never enter this file.
_SCHEMA_VERSION = 2
_FIELDS = ("version", "cwd", "model", "permission_mode", "effort", "provider")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _path(config_dir: Path | str, sid: str) -> Path | None:
    # ``sid`` is our own SDK session id (a uuid), but guard against traversal
    # anyway since it ends up in a filename.
    if not isinstance(sid, str) or not _SAFE_ID.match(sid):
        return None
    return Path(config_dir).expanduser() / "sessions" / f"{sid}.json"


def save(config_dir: Path | str, sid: str, **fields: Any) -> None:
    """Merge ``fields`` into the sidecar for ``sid`` (drops ``None`` values)."""
    path = _path(config_dir, sid)
    if path is None:
        return
    model_supplied = "model" in fields
    data = {k: v for k, v in fields.items() if k in _FIELDS and v is not None}
    if not data and not model_supplied:
        return
    existing = store.load(path) or {}
    # A v1 `model` may be a resolved transcript ID. Drop it during migration;
    # only the caller-provided v2 value below has launch-selection provenance.
    if existing.get("version") != _SCHEMA_VERSION:
        existing.pop("model", None)
    if model_supplied and fields["model"] is None:
        existing.pop("model", None)
    existing.update(data)
    existing["version"] = _SCHEMA_VERSION
    store.save(path, existing)


def load(config_dir: Path | str, sid: str) -> dict[str, Any]:
    """Return the saved settings for ``sid`` (``{}`` when none)."""
    path = _path(config_dir, sid)
    if path is None:
        return {}
    return store.load(path) or {}
