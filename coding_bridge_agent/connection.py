"""WebSocket client toward the coding-bridge relay.

Maintains a single outbound connection (``/ws/node?token=...``), heartbeats every
``heartbeat_interval`` seconds, reconnects with backoff, and dispatches browser
commands to local sessions. The node never accepts inbound connections.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from . import fs, history, protocol
from .config import Settings
from .protocol import Action, Event, event_payload
from .providers import KNOWN_PROVIDERS, default_provider_factory
from .providers.base import ProviderFactory
from .session import Session

logger = logging.getLogger("coding-bridge-agent.connection")


class AuthFailed(Exception):
    """The bridge rejected the node token; the user must re-pair."""


class BridgeConnection:
    def __init__(
        self,
        settings: Settings,
        node_token: str,
        *,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.settings = settings
        self.node_token = node_token
        self.provider_factory = provider_factory or default_provider_factory(settings)
        self.sessions: dict[str, Session] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()

    def capabilities(self) -> list[str]:
        return ["claude", "codex", "history"]

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        delay = self.settings.reconnect_min
        while not self._stop.is_set():
            try:
                await self._connect_once()
                delay = self.settings.reconnect_min
            except AuthFailed:
                logger.error("node token rejected by bridge; re-run `coding-bridge-agent pair`")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep retrying on transient errors
                logger.warning("bridge connection error: %s", exc)
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.settings.reconnect_max)

    async def _connect_once(self) -> None:
        url = f"{self.settings.ws_node_url}?token={self.node_token}"
        try:
            async with websockets.connect(
                url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ws:
                self._ws = ws
                logger.info("connected to bridge as node %s", self.node_token[:12])
                heartbeat = asyncio.create_task(self._heartbeat())
                try:
                    async for raw in ws:
                        await self._on_raw(raw)
                finally:
                    heartbeat.cancel()
                    self._ws = None
        except websockets.exceptions.ConnectionClosed as exc:
            if getattr(exc, "code", None) == 4401:
                raise AuthFailed() from exc
            raise
        except Exception as exc:  # noqa: BLE001 - classify handshake auth failures
            if _is_auth_error(exc):
                raise AuthFailed() from exc
            raise

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.heartbeat_interval)
                await self._send_envelope(
                    protocol.envelope(
                        protocol.NODE_HEARTBEAT, {"capabilities": self.capabilities()}
                    )
                )
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            return

    async def _send_envelope(self, env: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps(env))

    async def send_payload(self, payload: dict) -> None:
        """Send an inner event payload toward the owner's browsers."""
        await self._send_envelope(
            protocol.envelope(protocol.NODE_TO_BROWSER, payload, from_node=self.node_token)
        )

    async def _on_raw(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        msg_type = message.get("type")
        if msg_type == protocol.BROWSER_TO_NODE:
            await self._dispatch(message.get("payload") or {})
        elif msg_type == protocol.NODE_REGISTERED:
            logger.info("registered with bridge")

    async def _dispatch(self, payload: dict) -> None:
        action = payload.get("action")
        session_id = payload.get("session_id")
        try:
            if action == Action.SESSION_START:
                await self._start_session(payload)
            elif action == Action.SESSION_SEND:
                await self._send_to_session(
                    session_id, payload.get("prompt", ""), payload.get("images")
                )
            elif action == Action.SESSION_INTERRUPT:
                await self._interrupt_session(session_id)
            elif action == Action.SESSION_CLOSE:
                await self._close_session(session_id)
            elif action == Action.PERMISSION_RESOLVE:
                self._resolve_permission(payload)
            elif action == Action.SESSIONS_LIST:
                await self._send_snapshot()
            elif action == Action.HISTORY_LIST:
                await self._send_history_list(payload)
            elif action == Action.HISTORY_GET:
                await self._send_history_detail(payload)
            elif action == Action.FS_LIST:
                await self._send_fs_list(payload)
            elif action == Action.PING:
                await self.send_payload(event_payload(Event.PONG))
            else:
                await self.send_payload(
                    event_payload(
                        Event.SESSION_ERROR, session_id, message=f"unknown action: {action}"
                    )
                )
        except Exception as exc:  # noqa: BLE001 - report, never crash the node
            logger.warning("dispatch error (%s): %s", action, exc)
            await self.send_payload(
                event_payload(Event.SESSION_ERROR, session_id, message=str(exc))
            )

    async def _start_session(self, payload: dict) -> None:
        session_id = payload.get("session_id")
        if not session_id:
            await self.send_payload(
                event_payload(Event.SESSION_ERROR, None, message="session_id required")
            )
            return
        provider = payload.get("provider") or "claude"
        if provider not in KNOWN_PROVIDERS:
            await self.send_payload(
                event_payload(
                    Event.SESSION_ERROR,
                    session_id,
                    message=f"unsupported provider: {provider}",
                )
            )
            return
        existing = self.sessions.get(session_id)
        if existing is not None:
            await existing.send(payload.get("prompt", ""), payload.get("images"))
            return
        session = Session(
            session_id,
            self.provider_factory,
            self.send_payload,
            self.settings,
            cwd=payload.get("cwd") or self.settings.default_cwd,
            model=payload.get("model") or self.settings.default_model,
            permission_mode=payload.get("permission_mode") or "default",
            provider=provider,
            effort=payload.get("effort") or None,
            resume=payload.get("resume_session_id") or None,
        )
        self.sessions[session_id] = session
        await session.start(payload.get("prompt", ""), payload.get("images"))

    async def _send_to_session(
        self, session_id: str | None, prompt: str, images: list | None = None
    ) -> None:
        session = self.sessions.get(session_id) if session_id else None
        if session is None:
            await self.send_payload(
                event_payload(Event.SESSION_ERROR, session_id, message="unknown session")
            )
            return
        await session.send(prompt, images)

    async def _interrupt_session(self, session_id: str | None) -> None:
        session = self.sessions.get(session_id) if session_id else None
        if session is not None:
            await session.interrupt()

    async def _close_session(self, session_id: str | None) -> None:
        session = self.sessions.pop(session_id, None) if session_id else None
        if session is not None:
            await session.close()

    def _resolve_permission(self, payload: dict) -> None:
        request_id = payload.get("request_id")
        decision = "allow" if payload.get("decision") == "allow" else "deny"
        if not request_id:
            return
        for session in self.sessions.values():
            if session.resolve_permission(request_id, decision):
                break

    async def _send_snapshot(self) -> None:
        await self.send_payload(
            event_payload(
                Event.SESSIONS_SNAPSHOT,
                sessions=[s.info() for s in self.sessions.values()],
            )
        )

    async def _send_history_list(self, payload: dict) -> None:
        limit = payload.get("limit") or 200
        sessions = await asyncio.to_thread(history.list_sessions, limit)
        await self.send_payload(event_payload(Event.HISTORY_SNAPSHOT, sessions=sessions))

    async def _send_history_detail(self, payload: dict) -> None:
        provider = payload.get("provider")
        session_id = payload.get("session_id")
        if not provider or not session_id:
            await self.send_payload(
                event_payload(
                    Event.SESSION_ERROR, session_id, message="provider and session_id required"
                )
            )
            return
        detail = await asyncio.to_thread(history.read_session, provider, session_id)
        await self.send_payload(event_payload(Event.HISTORY_DETAIL, session_id, **detail))

    async def _send_fs_list(self, payload: dict) -> None:
        result = await asyncio.to_thread(
            fs.list_dir,
            payload.get("path"),
            show_hidden=bool(payload.get("show_hidden")),
        )
        await self.send_payload(event_payload(Event.FS_LIST, **result))

    async def aclose(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()


def _is_auth_error(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if code in (401, 403):
        return True
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) in (401, 403)
