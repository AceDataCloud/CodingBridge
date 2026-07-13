"""Thin async HTTP client for the WeChat gateway's REST API.

Kept intentionally minimal — the adapter (or future P7 abuse-control layer)
composes it. The client is stateless besides the ``httpx.AsyncClient`` it owns,
so tests can inject a transport via :func:`respx`.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from ..base import ChannelTarget, SendResult

# The gateway's ``send`` endpoint returns 202 Accepted with a task id. Anything else
# is treated as a delivery failure — the adapter surfaces the payload verbatim
# to the caller so operators have real diagnostics in the log.
_ACCEPTED_STATUSES = frozenset({200, 201, 202})

# Safe pattern for a gateway task id. The gateway generates UUID-like tokens; we
# refuse anything with URL metacharacters so a caller can't inject a query
# parameter or path segment into the GET.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class WeChatClient:
    """Async client for one WeChat gateway endpoint (one CVM / one WeChat instance)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        # Kept for the WS side (which needs the raw token as a query param) —
        # the client itself never logs or echoes it.
        self._token = token

    @property
    def token(self) -> str:
        return self._token

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:
        payload: dict[str, Any] = {
            "target": target.conversation_id,
            "text": text,
        }
        if target.conversation_type:
            payload["conversation_type"] = target.conversation_type
        if reply_to or target.reply_to_id:
            payload["reply_to"] = reply_to or target.reply_to_id

        started = time.perf_counter()
        try:
            resp = await self._client.post("/api/messages/send", json=payload)
        except httpx.HTTPError as exc:
            # Bound the exception message so a misbehaving library that echoes
            # request headers can't leak the Bearer token through str(exc).
            return SendResult(
                ok=False,
                error=f"transport error: {exc.__class__.__name__}: {str(exc)[:200]}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        upstream_id: str | None = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                task_id = body.get("task_id") or body.get("id") or body.get("data", {}).get("id")
                if isinstance(task_id, str) and task_id:
                    upstream_id = task_id
        except (ValueError, TypeError):
            body = None

        if resp.status_code in _ACCEPTED_STATUSES:
            return SendResult(ok=True, upstream_id=upstream_id, latency_ms=latency_ms)

        # Redact the token when constructing the error message — the token
        # never appears in the body, but any future header echo could leak
        # via ``resp.text``. The safe pattern is to include only status + a
        # bounded slice of the JSON error field.
        err = None
        if isinstance(body, dict):
            err = body.get("error") or body.get("detail") or body.get("message")
        return SendResult(
            ok=False,
            upstream_id=upstream_id,
            error=f"HTTP {resp.status_code}: {(err or '')[:200]}",
            latency_ms=latency_ms,
        )

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        """Fetch delivery status for a task returned by ``send_message``.

        Returns the parsed JSON body as-is (the gateway's shape may evolve). Raises
        ``httpx.HTTPStatusError`` on non-2xx so the caller can distinguish
        "not delivered yet" (200 with status=queued) from "task unknown" (404).

        Kept deliberately small: this is a debug / diagnostics primitive used
        by the ``doctor`` CLI command (P4) and E2E validation, not by the
        adapter's fast path.

        Raises ``ValueError`` on an empty or unsafely-shaped ``task_id`` —
        rather than URL-encode and pray, we refuse anything that could
        contain path/query metacharacters (``?``, ``#``, ``/``, ``..``).
        """
        if not task_id:
            raise ValueError("task_id must not be empty")
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError("task_id contains invalid characters")
        # Belt-and-suspenders: `quote(safe="")` still produces a plain
        # segment since the regex already rejects unsafe input.
        path = f"/api/tasks/{quote(task_id, safe='')}"
        resp = await self._client.get(path)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
