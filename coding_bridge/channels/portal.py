"""Local web portal for editing ``channels.toml`` — ``coding-bridge channels portal``.

Editing the WeChat channel config by hand is painful: you have to know each
admin's raw ``wxid`` and each group's ``…@chatroom`` id. This module serves a
small **localhost-only** web UI that talks to the WeChat gateway on your behalf
(contacts, groups, the bot account) so you can pick admins/groups from a
searchable, avatar-annotated list and flip the trigger mode, then writes the
result back to ``channels.toml``.

Security model (a config UI that can rewrite local config + reach a gateway
token must be defensive):

* **Binds 127.0.0.1 only** — never a public interface. ``serve()`` hard-codes it.
* **Host-header allowlist** — every request's ``Host`` must be loopback, which
  blocks DNS-rebinding (a remote page resolving a name to 127.0.0.1).
* **Per-session token** — a random token is printed to the console and embedded
  in the served page; every ``/api`` call must present it. A drive-by page on
  another origin can't read our HTML or our responses (same-origin policy), so
  it can't learn the token and can't drive the API.
* **The gateway token never reaches the browser** — it's resolved server-side
  from ``token_env`` / ``token_file`` and only used for outbound gateway calls.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import (
    ChannelsConfig,
    ConfigError,
    WeChatInstanceConfig,
    load_channels_config,
    parse_channels_config,
)
from .portal_html import INDEX_HTML

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger("coding-bridge.channels.portal")

# Cap POST bodies so a stray client can't make us buffer forever.
_MAX_BODY_BYTES = 256 * 1024
# Contacts are cached briefly so typing in the search box doesn't refetch 4k+
# rows on every keystroke.
_CONTACTS_TTL_SECONDS = 60.0
# Upper bound on contacts fetched from the gateway (4.5k+ real accounts) so one
# runaway gateway can't make us page forever.
_CONTACTS_MAX = 6000
# The WeChat gateway caps ``/api/contacts?limit`` at 200 (larger → HTTP 422).
_CONTACTS_PAGE = 200


# --------------------------------------------------------------------------- #
# TOML serialization (write side — the config loader only reads).
# --------------------------------------------------------------------------- #

_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _toml_str(value: str) -> str:
    """Serialize a Python str as a TOML basic string (quoted + escaped)."""
    out = ['"']
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _dump_instance(inst: WeChatInstanceConfig) -> str:
    lines = [
        "[[channels.wechat]]",
        f"instance_id = {_toml_str(inst.instance_id)}",
        f"base_url = {_toml_str(inst.base_url)}",
    ]
    if inst.token_env:
        lines.append(f"token_env = {_toml_str(inst.token_env)}")
    if inst.token_file:
        lines.append(f"token_file = {_toml_str(inst.token_file)}")
    lines.append(f"enabled = {'true' if inst.enabled else 'false'}")
    if inst.default_provider:
        lines.append(f"default_provider = {_toml_str(inst.default_provider)}")
    lines.append(f"trigger_prefix = {_toml_str(inst.trigger_prefix)}")
    senders = ", ".join(_toml_str(s) for s in inst.allowed_senders)
    lines.append(f"allowed_senders = [{senders}]")
    groups = ", ".join(_toml_str(s) for s in inst.allowed_groups)
    lines.append(f"allowed_groups = [{groups}]")
    lines.append(f"rate_limit_per_min = {inst.rate_limit_per_min}")
    # repr() on a float always yields a TOML-valid float literal (keeps the dot).
    lines.append(f"dedup_window_seconds = {inst.dedup_window_seconds!r}")
    return "\n".join(lines)


def dump_channels_toml(config: ChannelsConfig) -> str:
    """Render a :class:`ChannelsConfig` back to ``channels.toml`` text."""
    header = (
        "# coding-bridge channels config\n"
        "# Managed by `coding-bridge channels portal` — hand edits are preserved\n"
        "# in spirit but reformatted on the next portal save.\n"
    )
    if not config.wechat:
        return header
    body = "\n\n".join(_dump_instance(inst) for inst in config.wechat)
    return f"{header}\n{body}\n"


def _write_atomic(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` atomically, 0600 on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".channels-", suffix=".toml")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# --------------------------------------------------------------------------- #
# Public (token-free) JSON views of the config.
# --------------------------------------------------------------------------- #


def _instance_public(inst: WeChatInstanceConfig, *, resolvable: bool) -> dict[str, Any]:
    """JSON view of one instance — never includes the token value itself."""
    if inst.token_env:
        token_source = {"kind": "env", "ref": inst.token_env}
    elif inst.token_file:
        token_source = {"kind": "file", "ref": inst.token_file}
    else:
        token_source = {"kind": "none", "ref": None}
    return {
        "instance_id": inst.instance_id,
        "base_url": inst.base_url,
        "token_source": token_source,
        "token_resolvable": resolvable,
        "enabled": inst.enabled,
        "default_provider": inst.default_provider or "claude",
        "trigger_prefix": inst.trigger_prefix,
        "free_form": inst.trigger_prefix == "",
        "allowed_senders": list(inst.allowed_senders),
        "allowed_groups": list(inst.allowed_groups),
        "rate_limit_per_min": inst.rate_limit_per_min,
        "dedup_window_seconds": inst.dedup_window_seconds,
    }


class PortalError(Exception):
    """Raised by :class:`PortalService` with an HTTP status + safe message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class PortalService:
    """All portal logic, independent of the HTTP layer (so it's unit-testable)."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._path = settings.channels_config_path
        self._client = client or httpx.Client(timeout=15.0)
        self._owns_client = client is None
        self._contacts_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        # Per-instance single-flight lock so overlapping searches (and the
        # background warm) share ONE fetch instead of each paging 4k+ rows.
        self._fetch_locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ---- config ---------------------------------------------------------- #

    def _load(self) -> ChannelsConfig:
        try:
            return load_channels_config(self._path)
        except ConfigError as exc:
            raise PortalError(500, f"existing config is invalid: {exc}") from None

    def public_config(self) -> dict[str, Any]:
        cfg = self._load()
        instances = [
            _instance_public(inst, resolvable=self._token_resolvable(inst))
            for inst in cfg.wechat
        ]
        return {"config_path": str(self._path), "instances": instances}

    def _token_resolvable(self, inst: WeChatInstanceConfig) -> bool:
        try:
            inst.resolve_token()
            return True
        except ConfigError:
            return False

    def save(self, instances: list[dict[str, Any]]) -> dict[str, Any]:
        """Validate the posted instances, write ``channels.toml``, return the reload."""
        if not isinstance(instances, list):
            raise PortalError(400, "instances must be a list")
        # Round-trip through the real loader so the portal can never write a
        # config the daemon would then refuse to start with.
        blocks: list[dict[str, Any]] = []
        for item in instances:
            if not isinstance(item, dict):
                raise PortalError(400, "each instance must be an object")
            blocks.append(_instance_from_payload(item))
        try:
            cfg = parse_channels_config({"channels": {"wechat": blocks}})
        except ConfigError as exc:
            raise PortalError(400, str(exc)) from None
        try:
            _write_atomic(self._path, dump_channels_toml(cfg))
        except OSError as exc:
            # Never surface the path/traceback to the client — just the error kind.
            raise PortalError(500, f"could not write config: {exc.__class__.__name__}") from None
        with self._lock:
            self._contacts_cache.clear()
        return self.public_config()

    def _instance(self, instance_id: str) -> WeChatInstanceConfig:
        for inst in self._load().wechat:
            if inst.instance_id == instance_id:
                return inst
        raise PortalError(404, f"no such instance {instance_id!r}")

    # ---- WeChat gateway proxy ------------------------------------------- #

    def _gateway_request(
        self, inst: WeChatInstanceConfig, method: str, path: str, params: dict[str, Any]
    ) -> httpx.Response:
        try:
            token = inst.resolve_token()
        except ConfigError as exc:
            raise PortalError(400, str(exc)) from None
        url = f"{inst.base_url}{path}"
        try:
            return self._client.request(
                method, url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise PortalError(502, f"gateway unreachable: {exc.__class__.__name__}") from None

    def _gateway_get(self, inst: WeChatInstanceConfig, path: str, params: dict[str, Any]) -> Any:
        resp = self._gateway_request(inst, "GET", path, params)
        if resp.status_code in (401, 403):
            raise PortalError(502, "gateway rejected the token (check token_env/token_file)")
        if resp.status_code >= 400:
            raise PortalError(502, f"gateway returned {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise PortalError(502, "gateway returned non-JSON") from None

    def account(self, instance_id: str) -> dict[str, Any]:
        return self._gateway_get(self._instance(instance_id), "/api/account", {})

    def status(self, instance_id: str) -> dict[str, Any]:
        return self._gateway_get(self._instance(instance_id), "/api/auth/status", {})

    def qr(self, instance_id: str) -> dict[str, Any]:
        """Fetch a login QR (base64 PNG), resolving the gateway's async UI task.

        The gateway's ``/api/auth/qr`` returns a queued ``UiTaskOut``; the image
        lands in ``result`` once the task finishes, so we poll ``/api/tasks/{id}``
        server-side and hand the browser a ready-to-render data URL. Returns 409
        (surfaced as such) when the account is already logged in.
        """
        inst = self._instance(instance_id)
        resp = self._gateway_request(inst, "GET", "/api/auth/qr", {"type": "base64"})
        if resp.status_code == 409:
            raise PortalError(409, "already logged in")
        if resp.status_code in (401, 403):
            raise PortalError(502, "gateway rejected the token (check token_env/token_file)")
        if resp.status_code >= 400:
            raise PortalError(502, f"gateway returned {resp.status_code}")
        try:
            task = resp.json()
        except ValueError:
            raise PortalError(502, "gateway returned non-JSON") from None
        result = self._resolve_ui_task(inst, task)
        b64 = result.get("base64") if isinstance(result, dict) else None
        if not b64 or not isinstance(b64, str):
            raise PortalError(502, "gateway did not return a QR image")
        return {"base64": b64}

    def _resolve_ui_task(
        self, inst: WeChatInstanceConfig, task: Any, *, attempts: int = 25, delay: float = 0.4
    ) -> Any:
        """Return a UI task's ``result``, polling ``/api/tasks/{id}`` until it lands."""
        if not isinstance(task, dict):
            raise PortalError(502, "gateway returned a malformed task")
        if task.get("result"):
            return task["result"]
        task_id = task.get("id")
        if not task_id or not isinstance(task_id, str):
            raise PortalError(502, "gateway task has no id")
        for _ in range(attempts):
            time.sleep(delay)
            polled = self._gateway_get(inst, f"/api/tasks/{task_id}", {})
            if not isinstance(polled, dict):
                continue
            if polled.get("result"):
                return polled["result"]
            err = polled.get("error")
            if err:
                msg = err.get("message") if isinstance(err, dict) else None
                raise PortalError(502, f"gateway task failed: {msg or 'error'}")
        raise PortalError(504, "gateway QR task timed out")

    def groups(self, instance_id: str) -> list[dict[str, Any]]:
        inst = self._instance(instance_id)
        data = self._gateway_get(inst, "/api/conversations", {"limit": 200})
        convs = data.get("conversations", []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for c in convs:
            if isinstance(c, dict) and c.get("type") == "group":
                out.append(
                    {
                        "id": c.get("id"),
                        "name": c.get("name") or c.get("id"),
                        "avatar_url": c.get("avatar_url"),
                    }
                )
        return out

    def search_contacts(self, instance_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        inst = self._instance(instance_id)
        contacts = self._contacts(inst)
        q = query.strip().lower()
        hits = [c for c in contacts if q in c["_haystack"]] if q else contacts
        return [
            {k: v for k, v in c.items() if not k.startswith("_")} for c in hits[: max(0, limit)]
        ]

    def _contacts(self, inst: WeChatInstanceConfig) -> list[dict[str, Any]]:
        def _fresh() -> list[dict[str, Any]] | None:
            cached = self._contacts_cache.get(inst.instance_id)
            if cached and time.monotonic() - cached[0] < _CONTACTS_TTL_SECONDS:
                return cached[1]
            return None

        with self._lock:
            hit = _fresh()
            if hit is not None:
                return hit
            fetch_lock = self._fetch_locks.get(inst.instance_id)
            if fetch_lock is None:
                fetch_lock = threading.Lock()
                self._fetch_locks[inst.instance_id] = fetch_lock
        # Single-flight: the first caller fetches; concurrent callers block here
        # and then reuse the just-populated cache instead of refetching.
        with fetch_lock:
            with self._lock:
                hit = _fresh()
                if hit is not None:
                    return hit
            contacts = self._fetch_all_contacts(inst)
            with self._lock:
                self._contacts_cache[inst.instance_id] = (time.monotonic(), contacts)
            return contacts

    @staticmethod
    def _normalize_contacts(rows: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return out
        for c in rows:
            if not isinstance(c, dict):
                continue
            wxid = c.get("wechat_id") or ""
            nickname = c.get("nickname") or ""
            remark = c.get("remark") or ""
            alias = c.get("alias") or ""
            out.append(
                {
                    "wechat_id": wxid,
                    "nickname": nickname,
                    "remark": remark,
                    "avatar_url": c.get("avatar_url"),
                    "_haystack": f"{wxid} {nickname} {remark} {alias}".lower(),
                }
            )
        return out

    def _fetch_page(self, inst: WeChatInstanceConfig, offset: int) -> list[dict[str, Any]]:
        try:
            data = self._gateway_get(
                inst,
                "/api/contacts",
                {"limit": _CONTACTS_PAGE, "offset": offset, "type": "friend"},
            )
        except PortalError:
            # Best-effort: a single transient page failure shouldn't fail the
            # whole search — the target contact is likely on another page.
            return []
        return self._normalize_contacts(data.get("contacts") if isinstance(data, dict) else None)

    def _fetch_all_contacts(self, inst: WeChatInstanceConfig) -> list[dict[str, Any]]:
        first = self._gateway_get(
            inst, "/api/contacts", {"limit": _CONTACTS_PAGE, "offset": 0, "type": "friend"}
        )
        rows0 = first.get("contacts") if isinstance(first, dict) else None
        out = self._normalize_contacts(rows0)
        if not isinstance(rows0, list) or len(rows0) < _CONTACTS_PAGE:
            return out
        total = first.get("total") if isinstance(first, dict) else None
        hi = total if isinstance(total, int) and 0 < total <= _CONTACTS_MAX else _CONTACTS_MAX
        offsets = list(range(_CONTACTS_PAGE, hi, _CONTACTS_PAGE))
        if not offsets:
            return out
        # Fetch the remaining pages concurrently — the gateway caps limit at 200,
        # so a few thousand friends is otherwise ~20 serial round-trips.
        with ThreadPoolExecutor(max_workers=6) as pool:
            for rows in pool.map(lambda o: self._fetch_page(inst, o), offsets):
                out.extend(rows)
        return out


def _instance_from_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Map a browser payload to a raw ``[[channels.wechat]]`` dict.

    Only known keys are copied through; the real validator
    (``parse_channels_config``) then enforces types and constraints, so this
    stays a thin, allow-listed translation.
    """
    block: dict[str, Any] = {}
    for key in ("instance_id", "base_url", "token_env", "token_file", "default_provider"):
        val = item.get(key)
        if isinstance(val, str) and val:
            block[key] = val
    if "enabled" in item:
        block["enabled"] = bool(item["enabled"])
    # ``free_form`` is the UI-friendly form of ``trigger_prefix == ""``.
    if item.get("free_form") is True:
        block["trigger_prefix"] = ""
    elif isinstance(item.get("trigger_prefix"), str):
        block["trigger_prefix"] = item["trigger_prefix"]
    senders = item.get("allowed_senders")
    if isinstance(senders, list):
        block["allowed_senders"] = [s for s in senders if isinstance(s, str) and s]
    groups = item.get("allowed_groups")
    if isinstance(groups, list):
        block["allowed_groups"] = [s for s in groups if isinstance(s, str) and s]
    if isinstance(item.get("rate_limit_per_min"), int) and not isinstance(
        item.get("rate_limit_per_min"), bool
    ):
        block["rate_limit_per_min"] = item["rate_limit_per_min"]
    dedup = item.get("dedup_window_seconds")
    if isinstance(dedup, (int, float)) and not isinstance(dedup, bool):
        block["dedup_window_seconds"] = dedup
    return block


# --------------------------------------------------------------------------- #
# HTTP layer.
# --------------------------------------------------------------------------- #


def _make_handler(service: PortalService, token: str, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "coding-bridge-portal"

        def log_message(self, *_args: Any) -> None:  # silence default stderr spam
            return

        # -- helpers ------------------------------------------------------- #

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _token_ok(self, query: dict[str, list[str]]) -> bool:
            supplied = self.headers.get("X-Portal-Token") or (query.get("token", [""])[0])
            # compare_digest raises TypeError on a non-ASCII str — treat any such
            # malformed token as simply wrong rather than crashing the handler.
            try:
                return secrets.compare_digest(supplied, token)
            except (TypeError, ValueError):
                return False

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        # -- routing ------------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if not self._host_ok():
                self._send_json(403, {"error": "bad host"})
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                self._send_html(INDEX_HTML.replace("__PORTAL_TOKEN__", token))
                return
            if not path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            if not self._token_ok(query):
                self._send_json(401, {"error": "bad or missing portal token"})
                return
            self._handle_api_get(path, query)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send_json(403, {"error": "bad host"})
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._token_ok(query):
                self._send_json(401, {"error": "bad or missing portal token"})
                return
            if parsed.path != "/api/config":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json(400, {"error": "bad content-length"})
                return
            if length <= 0 or length > _MAX_BODY_BYTES:
                self._send_json(400, {"error": "empty or oversized body"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except ValueError:
                self._send_json(400, {"error": "invalid JSON"})
                return
            try:
                result = service.save((payload or {}).get("instances", []))
            except PortalError as exc:
                self._send_json(exc.status, {"error": exc.message})
                return
            except Exception:
                logger.exception("portal POST /api/config failed")
                self._send_json(500, {"error": "internal error"})
                return
            self._send_json(200, result)

        def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
            instance = (query.get("instance", [""])[0]).strip()
            try:
                if path == "/api/config":
                    self._send_json(200, service.public_config())
                elif path == "/api/wechat/account":
                    self._send_json(200, service.account(instance))
                elif path == "/api/wechat/status":
                    self._send_json(200, service.status(instance))
                elif path == "/api/wechat/qr":
                    self._send_json(200, service.qr(instance))
                elif path == "/api/wechat/groups":
                    self._send_json(200, {"groups": service.groups(instance)})
                elif path == "/api/wechat/contacts":
                    q = query.get("q", [""])[0]
                    try:
                        limit = int(query.get("limit", ["30"])[0])
                    except ValueError:
                        limit = 30
                    limit = min(max(limit, 1), 100)
                    self._send_json(
                        200, {"contacts": service.search_contacts(instance, q, limit)}
                    )
                else:
                    self._send_json(404, {"error": "not found"})
            except PortalError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except Exception:
                logger.exception("portal GET %s failed", path)
                self._send_json(500, {"error": "internal error"})

    return Handler


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Run the portal until Ctrl-C. Always binds loopback regardless of ``host``."""
    # Refuse anything but loopback — a config UI holding a gateway token must
    # never be reachable off-box.
    if host not in ("127.0.0.1", "localhost"):
        print(f"portal refuses to bind non-loopback host {host!r}; using 127.0.0.1")
        host = "127.0.0.1"

    service = PortalService(settings)
    token = secrets.token_urlsafe(24)
    handler = _make_handler(service, token, port)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"cannot bind {host}:{port}: {exc}")
        service.close()
        return 1

    url = f"http://127.0.0.1:{port}/?token={token}"
    print("Coding Bridge portal running — open this URL in your browser:")
    print(f"  {url}")
    print("(Loopback only. Keep the token secret. Press Ctrl-C to stop.)")
    if open_browser:
        import webbrowser

        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.close()
    return 0


__all__ = ["PortalService", "PortalError", "dump_channels_toml", "serve"]
