"""Thin async HTTP client for the WeChat gateway's REST API.

Kept intentionally minimal — the adapter (or future P7 abuse-control layer)
composes it. The client is stateless besides the ``httpx.AsyncClient`` it owns,
so tests can inject a transport via :func:`respx`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..base import ChannelTarget, SendResult

# The gateway's ``send`` endpoint returns 202 Accepted with a task id. Anything else
# is treated as a delivery failure — the adapter surfaces the payload verbatim
# to the caller so operators have real diagnostics in the log.
_ACCEPTED_STATUSES = frozenset({200, 201, 202})


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
