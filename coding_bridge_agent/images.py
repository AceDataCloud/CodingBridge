"""Decode browser-supplied images (base64 data URLs) into local temp files.

Both providers consume images as files on disk: Claude reads them via its Read
tool given the paths, and Codex attaches them with ``-i``. Files are written
under ``<cwd>/.tmp/images/<session>-<ts>/`` so a cwd-scoped agent can read them.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from pathlib import Path

# Reject absurdly large single images to bound memory/disk (~12 MB decoded).
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_DATA_URL = re.compile(r"^data:(?P<mime>[^;,]*?)(?P<b64>;base64)?,(?P<data>.*)$", re.DOTALL)
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


def save_images(images: list | None, cwd: str | None, *, session_id: str) -> list[str]:
    """Persist ``images`` and return the absolute paths actually written.

    Each item may be a data-URL / raw-base64 string, or a dict carrying
    ``data``/``url`` plus optional ``name`` and ``media_type``/``mime``. Invalid
    entries are skipped rather than failing the whole turn.
    """
    if not images:
        return []
    root = Path(cwd or ".").expanduser()
    folder = root / ".tmp" / "images" / f"{session_id}-{int(time.time() * 1000)}"
    paths: list[str] = []
    for index, item in enumerate(images):
        raw, mime, name = _extract(item)
        if not raw:
            continue
        try:
            blob = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            continue
        if not blob or len(blob) > MAX_IMAGE_BYTES:
            continue
        filename = _safe_name(name, mime, index)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / filename
        try:
            target.write_bytes(blob)
        except OSError:
            continue
        paths.append(str(target))
    return paths


def _extract(item: object) -> tuple[str | None, str | None, str | None]:
    if isinstance(item, str):
        return _split_data_url(item) + (None,)
    if isinstance(item, dict):
        source = item.get("data") or item.get("url") or item.get("base64") or ""
        name = item.get("name") or item.get("filename")
        data, mime = _split_data_url(str(source))
        mime = item.get("media_type") or item.get("mime") or mime
        return data, mime, name
    return None, None, None


def _split_data_url(value: str) -> tuple[str | None, str | None]:
    value = value.strip()
    if not value:
        return None, None
    match = _DATA_URL.match(value)
    if match:
        return (match.group("data") or "").strip(), (match.group("mime") or "").strip() or None
    # Bare base64 with no data: prefix.
    return value, None


def _safe_name(name: str | None, mime: str | None, index: int) -> str:
    ext = _EXT_BY_MIME.get((mime or "").lower(), "")
    if name:
        # Strip any directory components to prevent path traversal.
        base = Path(name).name
        if base and base not in (".", ".."):
            if "." in base:
                return base
            return base + (ext or ".png")
    return f"image_{index + 1}{ext or '.png'}"
