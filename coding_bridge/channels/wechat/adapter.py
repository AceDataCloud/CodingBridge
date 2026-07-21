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
import time
from collections import deque
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets

from ..base import ChannelTarget, IncomingMessage, MessageHandler, SendResult
from .client import WeChatClient

logger = logging.getLogger("coding-bridge.channels.wechat")

_INITIAL_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 30.0
_POLL_INTERVAL_S = 1.0
_MESSAGE_SEEN_MAX = 5000
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

    message_id = data.get("msg_id") if isinstance(data.get("msg_id"), str) else None
    upstream_id = f"{target}:{message_id}" if message_id else None
    return IncomingMessage(
        sender_id=str(data.get("sender_id") or target),
        sender_name=data.get("sender_name") if isinstance(data.get("sender_name"), str) else None,
        target=ChannelTarget(
            conversation_id=target,
            conversation_type=str(data.get("conversation_type") or "private"),
            reply_to_id=message_id,
        ),
        text=text,
        msg_type=str(data.get("msg_type") or "text"),
        direction=direction,
        upstream_id=upstream_id,
        received_at_ms=int(data["timestamp"]) if isinstance(data.get("timestamp"), int) else None,
        raw=dict(data),
    )


def _parse_polled_message(
    data: dict[str, Any],
    targets: dict[str, tuple[str, str]],
) -> IncomingMessage | None:
    if data.get("direction") != "inbound":
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text:
        return None
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    mapped_target = targets.get(conversation_id)
    if mapped_target is None:
        return None
    target_name = data.get("conversation_name")
    if not isinstance(target_name, str) or not target_name:
        target_name = mapped_target[0]
    message_id = str(data.get("id") or "")
    if not message_id:
        return None
    sent_at = data.get("sent_at") if isinstance(data.get("sent_at"), str) else ""
    upstream_id = f"{conversation_id}:{message_id}"
    received_at_ms = None
    if sent_at:
        with contextlib.suppress(ValueError):
            timestamp = datetime.fromisoformat(sent_at.replace("Z", "+00:00")).timestamp()
            received_at_ms = int(timestamp * 1000)
    conversation_type = mapped_target[1]
    return IncomingMessage(
        sender_id=str(data.get("sender_id") or conversation_id),
        sender_name=data.get("sender_name") if isinstance(data.get("sender_name"), str) else None,
        target=ChannelTarget(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            extra={"send_target": target_name},
        ),
        text=text,
        msg_type=str(data.get("type") or "text"),
        direction="inbound",
        upstream_id=upstream_id,
        received_at_ms=received_at_ms,
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
        poll_interval: float = _POLL_INTERVAL_S,
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
        self._poll_interval = poll_interval
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        # Held for the lifetime of one WS session so ``aclose()`` can force
        # the receive loop to unblock without waiting for a gateway heartbeat.
        self._active_ws: Any | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def run(self) -> None:
        if self._handler is None:
            raise RuntimeError("WeChatAdapter.run() called before set_handler()")
        poll_task = asyncio.create_task(
            self._poll_loop(),
            name=f"wechat-poll-{self.instance_id}",
        )
        try:
            await self._run_ws()
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

    async def _run_ws(self) -> None:
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

    async def _poll_loop(self) -> None:
        cursor = int(time.time()) - 1
        delay = self._poll_interval
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

            try:
                rows = await self._client.poll_messages(cursor, limit=500)
                if not rows:
                    delay = self._poll_interval
                    continue
                newest = cursor
                for row in rows:
                    sent_at = row.get("sent_at")
                    if isinstance(sent_at, str):
                        with contextlib.suppress(ValueError):
                            newest = max(
                                newest,
                                int(
                                    datetime.fromisoformat(
                                        sent_at.replace("Z", "+00:00")
                                    ).timestamp()
                                ),
                            )
                unseen_rows = [
                    row
                    for row in rows
                    if (
                        row.get("conversation_id")
                        and row.get("id") is not None
                        and f"{row['conversation_id']}:{row['id']}" not in self._seen
                    )
                ]
                conversations = await self._client.list_conversations() if unseen_rows else []
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "wechat: poll error instance=%s err=%s.%s",
                    self.instance_id,
                    exc.__class__.__module__,
                    exc.__class__.__name__,
                )
                delay = min(max(delay * 2, self._poll_interval), _MAX_BACKOFF_S)
                continue

            delay = self._poll_interval

            targets = {
                str(item["id"]): (str(item["name"]), str(item.get("type") or "private"))
                for item in conversations
                if item.get("id") and item.get("name")
            }
            parsed: list[IncomingMessage] = []
            for row in unseen_rows:
                message = _parse_polled_message(row, targets)
                if message is not None:
                    parsed.append(message)

            parsed.sort(key=lambda item: item.received_at_ms or 0)
            for message in parsed:
                await self._dispatch(message, source="poll")

            cursor = max(cursor, min(newest, int(time.time()) - 1))

    async def _dispatch(self, message: IncomingMessage, *, source: str) -> None:
        identity = message.upstream_id
        if identity and identity in self._seen:
            return
        if identity:
            self._seen.add(identity)
            self._seen_order.append(identity)
            while len(self._seen_order) > _MESSAGE_SEEN_MAX:
                self._seen.discard(self._seen_order.popleft())
        assert self._handler is not None
        try:
            await self._handler(message, self)
        except Exception:
            logger.exception(
                "wechat: %s handler error instance=%s",
                source,
                self.instance_id,
            )

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
            await self._dispatch(msg, source="WS")

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
