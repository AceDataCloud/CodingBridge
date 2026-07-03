"""Async Telegram Bot API client (long-polling). Mirrors the WeChat client shape.

The bot token lives in the URL **path** (``/bot<token>/<method>``), so it must
never be logged — this client never echoes the base URL or token in errors, and
callers should log ``TelegramError.error_code`` (an int), not the message alone.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx

from ..base import ChannelTarget, SendResult

# Telegram hard-caps a single message at 4096 chars (after entity parsing).
_MAX_TEXT = 4096


class TelegramError(Exception):
    """A Telegram API ``{ok: false}`` response or a transport failure.

    ``error_code`` carries Telegram's numeric code (401/403/409/429/…) so the
    adapter's poll loop can react (delete webhook on 409, honor ``retry_after``
    on 429, stop on 401). The message is bounded and token-free.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramClient:
    """Async client for one Telegram bot token."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        # The base URL embeds the secret token path segment — never logged.
        self._client = httpx.AsyncClient(
            base_url=f"{self._api_base}/bot{token}",
            timeout=timeout,
            transport=transport,
        )

    @property
    def token(self) -> str:
        return self._token

    @property
    def api_base(self) -> str:
        return self._api_base

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._client.post(f"/{method}", **kwargs)
        except httpx.HTTPError as exc:
            # Bound + type-only: a library that echoes the request URL must not
            # leak the token path segment through str(exc).
            raise TelegramError(f"transport error: {exc.__class__.__name__}") from None
        try:
            body = resp.json()
        except (ValueError, TypeError):
            raise TelegramError(
                f"non-JSON response (HTTP {resp.status_code})", error_code=resp.status_code
            ) from None
        if isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        code = body.get("error_code") if isinstance(body, dict) else resp.status_code
        desc = body.get("description") if isinstance(body, dict) else ""
        retry_after: float | None = None
        params = body.get("parameters") if isinstance(body, dict) else None
        if isinstance(params, dict) and isinstance(params.get("retry_after"), (int, float)):
            retry_after = float(params["retry_after"])
        raise TelegramError(
            (str(desc)[:200] or f"telegram error {code}"),
            error_code=int(code) if isinstance(code, int) else None,
            retry_after=retry_after,
        )

    async def get_me(self) -> dict[str, Any]:
        """Return the bot's own account (``id``/``username``/…). Used by doctor."""
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    async def get_updates(self, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        """Long-poll for new updates. Server holds up to ``poll_timeout`` seconds."""
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": poll_timeout, "allowed_updates": ["message"]},
            # Give httpx headroom over the server-side long-poll window.
            timeout=float(poll_timeout) + 15.0,
        )
        return result if isinstance(result, list) else []

    async def delete_webhook(self) -> None:
        """Drop any webhook so ``getUpdates`` works (recovery from HTTP 409)."""
        await self._call("deleteWebhook", {})

    async def send_message(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:
        payload: dict[str, Any] = {"chat_id": target.conversation_id, "text": text[:_MAX_TEXT]}
        rt = reply_to or target.reply_to_id
        if rt:
            with contextlib.suppress(TypeError, ValueError):
                payload["reply_to_message_id"] = int(rt)
        started = time.perf_counter()
        try:
            result = await self._call("sendMessage", payload)
        except TelegramError as exc:
            return SendResult(
                ok=False,
                error=f"{exc.__class__.__name__}: {str(exc)[:200]}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        upstream_id: str | None = None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            upstream_id = str(result["message_id"])
        return SendResult(ok=True, upstream_id=upstream_id, latency_ms=latency_ms)


__all__ = ["TelegramClient", "TelegramError"]
