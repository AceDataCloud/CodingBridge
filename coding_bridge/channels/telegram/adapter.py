"""``TelegramAdapter`` — long-polls the Telegram Bot API and dispatches messages.

Like the WeChat adapter it's a *pure wire*: it runs a ``getUpdates`` long-poll
loop (outbound HTTPS only — no inbound ports, no webhook), turns each ``message``
update into an :class:`IncomingMessage`, and hands it to the dispatcher's
handler. Policy (trigger prefix, sender/group allowlist, rate limit, dedup) lives
in :class:`coding_bridge.channels.policy.PolicyGate`, shared with WeChat.

Design notes:

* One poll loop per instance — Telegram's ``getUpdates`` is undefined under
  concurrent callers, so we never run two.
* ``offset`` advances to ``last update_id + 1`` to acknowledge updates.
* Recovers from a stray webhook (HTTP 409) by calling ``deleteWebhook``; honors
  ``retry_after`` on 429; stops on 401 (bad token). Other errors back off
  (0.5s → 30s).
* The token only ever lives in the client's URL path and is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..base import ChannelTarget, IncomingMessage, MessageHandler, SendResult
from .client import TelegramClient, TelegramError

logger = logging.getLogger("coding-bridge.channels.telegram")

_INITIAL_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 30.0
_DEFAULT_POLL_TIMEOUT_S = 30


def _parse_update(update: dict[str, Any]) -> IncomingMessage | None:
    """Turn one Telegram update into an ``IncomingMessage`` (or ``None`` to skip).

    Only plain-text ``message`` updates are handled — edits, channel posts,
    callbacks and non-text messages are dropped.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None
    frm = message.get("from") if isinstance(message.get("from"), dict) else {}
    sender_raw = frm.get("id")
    sender_id = str(sender_raw) if isinstance(sender_raw, int) else str(chat_id)
    name = frm.get("username") or frm.get("first_name")
    # Telegram: private == 1:1 DM; group/supergroup/channel are "group"-like so
    # the shared allowed_groups gate keys off conversation_type == "group".
    conv_type = "private" if chat.get("type") == "private" else "group"
    message_id = message.get("message_id")
    update_id = update.get("update_id")
    return IncomingMessage(
        sender_id=sender_id,
        sender_name=name if isinstance(name, str) else None,
        target=ChannelTarget(
            conversation_id=str(chat_id),
            conversation_type=conv_type,
            reply_to_id=str(message_id) if isinstance(message_id, int) else None,
        ),
        text=text,
        msg_type="text",
        direction="inbound",
        upstream_id=str(update_id) if isinstance(update_id, int) else None,
        received_at_ms=(
            int(message["date"]) * 1000 if isinstance(message.get("date"), int) else None
        ),
        raw=dict(message),
    )


class TelegramAdapter:
    """One Telegram bot token = one instance = one long-poll loop."""

    name = "telegram"

    def __init__(
        self,
        *,
        instance_id: str,
        token: str,
        api_base: str = "https://api.telegram.org",
        client: TelegramClient | None = None,
        stop_event: asyncio.Event | None = None,
        poll_timeout: int = _DEFAULT_POLL_TIMEOUT_S,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id must be a non-empty string")
        if not token:
            raise ValueError("token must be a non-empty string")
        self.instance_id = instance_id
        self._client = client or TelegramClient(token, api_base=api_base)
        self._owns_client = client is None
        self._stop = stop_event or asyncio.Event()
        self._handler: MessageHandler | None = None
        self._poll_timeout = poll_timeout

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def run(self) -> None:
        if self._handler is None:
            raise RuntimeError("TelegramAdapter.run() called before set_handler()")
        logger.info("telegram: polling instance=%s", self.instance_id)
        offset = 0
        backoff = _INITIAL_BACKOFF_S
        while not self._stop.is_set():
            try:
                updates = await self._client.get_updates(offset, self._poll_timeout)
                backoff = _INITIAL_BACKOFF_S
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    uid = update.get("update_id")
                    if isinstance(uid, int):
                        offset = uid + 1
                    if self._stop.is_set():
                        return
                    msg = _parse_update(update)
                    if msg is None:
                        continue
                    try:
                        await self._handler(msg, self)
                    except Exception:
                        # A single handler failure must not kill the poll loop.
                        logger.exception("telegram: handler error instance=%s", self.instance_id)
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                if self._stop.is_set():
                    return
                if exc.error_code == 409:
                    logger.warning(
                        "telegram: 409 conflict instance=%s (webhook set); deleting it",
                        self.instance_id,
                    )
                    with contextlib.suppress(Exception):
                        await self._client.delete_webhook()
                    continue
                if exc.error_code == 401:
                    logger.error(
                        "telegram: unauthorized instance=%s (check the bot token); stopping",
                        self.instance_id,
                    )
                    return
                wait = exc.retry_after if (exc.error_code == 429 and exc.retry_after) else backoff
                logger.warning(
                    "telegram: api error instance=%s code=%s; retry in %.1fs",
                    self.instance_id,
                    exc.error_code,
                    wait,
                )
                if await self._sleep_or_stop(wait):
                    return
                if exc.error_code != 429:
                    backoff = min(backoff * 2, _MAX_BACKOFF_S)
            except Exception as exc:
                if self._stop.is_set():
                    return
                logger.warning(
                    "telegram: poll error instance=%s err=%s; retry in %.1fs",
                    self.instance_id,
                    exc.__class__.__name__,
                    backoff,
                )
                if await self._sleep_or_stop(backoff):
                    return
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep, or return True early if a stop was signaled."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def send(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:
        return await self._client.send_message(target, text, reply_to=reply_to)

    async def aclose(self) -> None:
        """Idempotent shutdown. Closing the client interrupts an in-flight poll."""
        self._stop.set()
        if self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()


__all__ = ["TelegramAdapter"]
