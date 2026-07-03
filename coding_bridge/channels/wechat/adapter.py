"""``WeChatAdapter`` — subscribes to the WeChat gateway's WS feed and dispatches messages.

The adapter is a *pure wire*: it opens the WSS connection, decodes
``message.new`` events, filters out anything that isn't a real inbound WeChat
message (echoes, outbound, non-text types), and hands the rest to the
dispatcher-installed handler. Policy — trigger prefix, sender allowlist, rate
limit, dedup — lives in the abuse-control layer landing with P7.

Design notes:

* Reconnect loop uses bounded exponential backoff (0.5s → 30s) so a gateway
  restart doesn't take the adapter down.
* The token appears only in the WS query string and the REST client's
  ``Authorization`` header. Logs redact both — every URL echoed to the logs
  runs through :func:`_redact_url` first, and the REST client never echoes
  the token in error messages.
* ``aclose()`` is idempotent and reentrant — closing an already-closed
  adapter is a no-op and never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets

from ..base import ChannelTarget, IncomingMessage, MessageHandler, SendResult
from .client import WeChatClient

logger = logging.getLogger("coding-bridge.channels.wechat")

_INITIAL_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 30.0
_HTTP_TO_WS = {"http": "ws", "https": "wss"}
# Any query-string token or password segment is redacted before logging.
_TOKEN_QS_RE = re.compile(r"([?&](?:token|api_token|password)=)([^&]+)", re.IGNORECASE)
# ``user:pass@`` in the netloc is also stripped — a misconfigured base_url with
# embedded credentials would otherwise survive query-string redaction.
_USERINFO_RE = re.compile(r"(?<=://)([^/@]+)@")


def _redact_url(url: str) -> str:
    """Strip token / password from URL query string + userinfo for safe logging."""
    return _USERINFO_RE.sub("<redacted>@", _TOKEN_QS_RE.sub(r"\1<redacted>", url))


def _build_ws_url(base_url: str, token: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = _HTTP_TO_WS.get(parsed.scheme, parsed.scheme or "ws")
    path = (parsed.path or "").rstrip("/") + "/ws"
    return urlunparse((scheme, parsed.netloc, path, "", f"token={token}", ""))


def _parse_incoming(payload: dict[str, Any]) -> IncomingMessage | None:
    """Best-effort parser for the gateway's ``message.new`` event.

    Returns ``None`` for anything the dispatcher should never see (outbound
    echoes, unknown event types, missing required fields). Downstream policy
    (allowlist / trigger prefix) runs against the returned message.
    """
    event = payload.get("event")
    data = payload.get("data")
    if event != "message.new" or not isinstance(data, dict):
        return None
    direction = data.get("direction")
    if direction != "inbound":
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text:
        return None
    target = data.get("target")
    if not isinstance(target, str) or not target:
        return None

    return IncomingMessage(
        sender_id=str(data.get("sender_id") or target),
        sender_name=data.get("sender_name") if isinstance(data.get("sender_name"), str) else None,
        target=ChannelTarget(
            conversation_id=target,
            conversation_type=str(data.get("conversation_type") or "private"),
            reply_to_id=data.get("msg_id") if isinstance(data.get("msg_id"), str) else None,
        ),
        text=text,
        msg_type=str(data.get("msg_type") or "text"),
        direction=direction,
        upstream_id=data.get("msg_id") if isinstance(data.get("msg_id"), str) else None,
        received_at_ms=int(data["timestamp"]) if isinstance(data.get("timestamp"), int) else None,
        raw=dict(data),
    )


class WeChatAdapter:
    """One WeChat gateway endpoint = one WeChat instance = one adapter.

    Runs a WSS receive loop; delivery is done via the injected
    :class:`WeChatClient`. Only intended for private / group WeChat text
    messages — image / file / audio events are silently dropped in P2 (P8+
    can add multi-modal handling once the dispatcher supports attachments).
    """

    name = "wechat"

    def __init__(
        self,
        *,
        instance_id: str,
        base_url: str,
        token: str,
        client: WeChatClient | None = None,
        ws_connect: Any | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id must be a non-empty string")
        if not base_url:
            raise ValueError("base_url must be a non-empty string")
        if not token:
            raise ValueError("token must be a non-empty string")
        self.instance_id = instance_id
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or WeChatClient(base_url, token)
        # ``ws_connect`` is injected only by tests; production uses
        # :func:`websockets.connect` directly.
        self._ws_connect = ws_connect or websockets.connect
        self._stop = stop_event or asyncio.Event()
        self._handler: MessageHandler | None = None
        self._owns_client = client is None
        # Held for the lifetime of one WS session so ``aclose()`` can force
        # the receive loop to unblock without waiting for a gateway heartbeat.
        self._active_ws: Any | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def run(self) -> None:
        if self._handler is None:
            raise RuntimeError("WeChatAdapter.run() called before set_handler()")
        ws_url = _build_ws_url(self._base_url, self._token)
        backoff = _INITIAL_BACKOFF_S
        while not self._stop.is_set():
            try:
                logger.info(
                    "wechat: connecting instance=%s url=%s",
                    self.instance_id,
                    _redact_url(ws_url),
                )
                async with self._ws_connect(ws_url) as ws:
                    logger.info("wechat: connected instance=%s", self.instance_id)
                    backoff = _INITIAL_BACKOFF_S
                    self._active_ws = ws
                    try:
                        await self._consume(ws)
                    finally:
                        self._active_ws = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning(
                    "wechat: WS loop error instance=%s err=%s.%s; reconnect in %.1fs",
                    self.instance_id,
                    exc.__class__.__module__,
                    exc.__class__.__name__,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return  # stop signaled during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _consume(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (ValueError, TypeError):
                logger.debug("wechat: skipped non-JSON frame instance=%s", self.instance_id)
                continue
            if not isinstance(payload, dict):
                continue
            msg = _parse_incoming(payload)
            if msg is None:
                continue
            assert self._handler is not None  # invariant checked in run()
            try:
                await self._handler(msg, self)
            except Exception:
                # A single handler failure must not kill the receive loop —
                # the dispatcher already logs its own errors, and abusive
                # senders shouldn't be able to DoS the adapter.
                logger.exception("wechat: handler error instance=%s", self.instance_id)

    async def send(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:
        return await self._client.send_message(target, text, reply_to=reply_to)

    async def aclose(self) -> None:
        """Idempotent shutdown; safe to call from multiple coroutines."""
        self._stop.set()
        ws = self._active_ws
        if ws is not None:
            close = getattr(ws, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
        if self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
