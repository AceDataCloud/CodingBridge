"""Download browser-uploaded CDN attachments into cwd-scoped temp files."""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
ALLOWED_HOSTS = {"cdn.acedata.cloud", "platform.cdn.acedata.cloud"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
}
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def save_attachments(
    attachments: list | None,
    cwd: str | None,
    *,
    session_id: str,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    """Download trusted CDN attachments and return local file descriptors."""
    if not attachments:
        return []
    root = Path(cwd or ".").expanduser()
    folder = root / ".tmp" / "attachments" / f"{session_id}-{int(time.time() * 1000)}"
    results: list[dict[str, str]] = []
    own_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=30, trust_env=False)
    try:
        for index, item in enumerate(attachments[:MAX_ATTACHMENTS]):
            prepared = _download_one(http, item, folder, index)
            if prepared:
                results.append(prepared)
    finally:
        if own_client:
            http.close()
    return results


def attachment_note(
    prompt: str, files: list[dict[str, str]], image_paths: list[str] | None = None
) -> str:
    """Append a concise path list so coding agents can inspect attachments."""
    lines: list[str] = []
    for index, path in enumerate(image_paths or [], start=1):
        lines.append(f"image {index}: {path}")
    for item in files:
        label = "image" if item.get("kind") == "image" else "file"
        name = item.get("name") or Path(item["path"]).name
        lines.append(f"{label}: {name} -> {item['path']}")
    if not lines:
        return prompt
    note = "[Attachments saved on the local machine:]\n" + "\n".join(lines)
    return f"{prompt}\n\n{note}" if prompt else note


def image_paths(files: list[dict[str, str]]) -> list[str]:
    return [item["path"] for item in files if item.get("kind") == "image"]


def _download_one(
    client: httpx.Client, item: object, folder: Path, index: int
) -> dict[str, str] | None:
    url, name, mime, declared_kind = _extract(item)
    if not url or not _is_allowed_url(url):
        return None
    try:
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                return None
            header_mime = response.headers.get("content-type", "").split(";", 1)[0].strip()
            mime = mime or header_mime or None
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_ATTACHMENT_BYTES:
                return None
            blob = bytearray()
            for chunk in response.iter_bytes():
                blob.extend(chunk)
                if len(blob) > MAX_ATTACHMENT_BYTES:
                    return None
    except (OSError, ValueError, httpx.HTTPError):
        return None
    if not blob:
        return None
    kind = _kind(declared_kind, mime, name or url)
    filename = _safe_name(name or _name_from_url(url), mime, kind, index)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    try:
        target.write_bytes(bytes(blob))
    except OSError:
        return None
    return {"path": str(target), "name": filename, "kind": kind, "mime": mime or ""}


def _extract(item: object) -> tuple[str | None, str | None, str | None, str | None]:
    if isinstance(item, str):
        return item, None, None, None
    if isinstance(item, dict):
        url = item.get("url") or item.get("file_url") or item.get("image_url")
        name = item.get("name") or item.get("filename")
        mime = item.get("mime_type") or item.get("media_type") or item.get("mime")
        kind = item.get("type") or item.get("kind")
        return (
            str(url) if url else None,
            str(name) if name else None,
            str(mime) if mime else None,
            str(kind) if kind else None,
        )
    return None, None, None, None


def _is_allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in ALLOWED_HOSTS


def _kind(declared: str | None, mime: str | None, name: str) -> str:
    if declared == "image" or (mime or "").lower().startswith("image/"):
        return "image"
    return "image" if Path(name).suffix.lower() in _IMAGE_EXTS else "file"


def _name_from_url(url: str) -> str | None:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    return name or None


def _safe_name(name: str | None, mime: str | None, kind: str, index: int) -> str:
    ext = _EXT_BY_MIME.get((mime or "").lower(), "")
    fallback_ext = ext or (".png" if kind == "image" else ".bin")
    if name:
        base = _SAFE_CHARS.sub("_", Path(name).name).strip(" .")
        if base and base not in (".", ".."):
            return base if Path(base).suffix else base + fallback_ext
    return f"attachment_{index + 1}{fallback_ext}"
