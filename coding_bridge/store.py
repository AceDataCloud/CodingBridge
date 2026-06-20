"""Persistent credential storage (``~/.ace-bridge/credentials.json``)."""
from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def clear(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
