"""WebSocket client toward the coding-bridge relay.

Maintains a single outbound connection (``/ws/node?token=...``), heartbeats every
``heartbeat_interval`` seconds, reconnects with backoff, and dispatches browser
commands to local sessions. The node never accepts inbound connections.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque

import websockets

from . import capabilities, fs, history, logs, protocol, session_meta
from .config import Settings
from .protocol import Action, Event, event_payload
from .providers import KNOWN_PROVIDERS, default_provider_factory
from .providers.base import ProviderFactory
from .session import Session

logger = logging.getLogger("coding-bridge.connection")


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
        # Live sessions, keyed by their canonical id. A session opens under the
        # provisional id the browser minted, then is re-keyed to the provider's
        # real (SDK/transcript) id once known, so a resume-from-history reattaches
        # to it instead of spawning a parallel client. `_aliases` maps the old
        # provisional id to the real one for the brief window a command addressed
        # to the provisional id may still arrive.
        self.sessions: dict[str, Session] = {}
        self._aliases: dict[str, str] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log_forwarder: logs.BridgeLogForwarder | None = None
        # Reliable delivery: durable live events get a monotonic node_seq and sit
        # in the outbox until the relay acks them, so a reconnect resends the tail
        # instead of dropping it. node_seq is per node process (never reset); the
        # relay dedups resends by envelope id.
        self._node_seq = 0
        self._outbox: deque[dict] = deque()
        self._truncated_sessions: set[str] = set()
        # Dedup browser commands the relay may redeliver (a command queued across
        # our reconnect, then flushed). Bounded ring of recently seen cmd_ids.
        self._seen_cmds: set[str] = set()
        self._seen_cmd_order: deque[str] = deque()

    def capabilities(self) -> list[str]:
        # Reflect the providers whose CLI is actually installed, plus history.
        providers = [
            p["name"] for p in capabilities.describe(self.settings)["providers"] if p["available"]
        ]
        return [*providers, "history"]

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        delay = self.settings.reconnect_min
        while not self._stop.is_set():
            try:
                await self._connect_once()
                delay = self.settings.reconnect_min
            except AuthFailed:
                logger.error("node token rejected by bridge; re-run `coding-bridge pair`")
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
        self._loop = asyncio.get_running_loop()
        try:
            async with websockets.connect(
                url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ws:
                self._ws = ws
                logger.info("connected to bridge as node %s", self.node_token[:12])
                self._attach_log_forwarder()
                # Resend any live events the relay never acked before the drop.
                await self._flush_outbox()
                heartbeat = asyncio.create_task(self._heartbeat())
                try:
                    async for raw in ws:
                        await self._on_raw(raw)
                finally:
                    heartbeat.cancel()
                    self._detach_log_forwarder()
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
        """Transmit one envelope if connected; a no-op (drop) while offline.

        Heartbeats and logs use this directly — losing them across a reconnect is
        harmless. Durable browser events go through :meth:`send_payload`, which
        buffers them in the outbox so the drop here is recovered on reconnect.
        """
        ws = self._ws
        if ws is None:
            return
        # Best-effort: a send racing a socket close must not crash the daemon;
        # durable events remain in the outbox and resend on the next connect.
        with contextlib.suppress(Exception):  # noqa: BLE001
            await ws.send(json.dumps(env))

    async def send_payload(self, payload: dict) -> None:
        """Send an inner event payload toward the owner's browsers.

        Durable live events (a session's streamed output, permissions, rewind)
        get a monotonic ``node_seq`` and are buffered in the outbox until the
        relay acks them — so a reconnect resends the tail rather than losing it.
        Request-scoped responses (history/fs/capabilities snapshots) are sent
        fire-and-forget; a reconnecting browser re-requests those itself.
        """
        env = protocol.envelope(protocol.NODE_TO_BROWSER, payload, from_node=self.node_token)
        if protocol.is_durable_event(payload):
            self._node_seq += 1
            env["node_seq"] = self._node_seq
            self._outbox.append(env)
            self._trim_outbox()
        await self._send_envelope(env)

    async def _flush_outbox(self) -> None:
        """On (re)connect, warn about any overflow gap, then resend the outbox."""
        for session_id in list(self._truncated_sessions):
            await self._send_envelope(
                protocol.envelope(
                    protocol.NODE_TO_BROWSER,
                    event_payload(
                        Event.SESSION_STREAM_TRUNCATED,
                        session_id,
                        reason="node_outbox_overflow",
                        resume_with="history",
                    ),
                    from_node=self.node_token,
                )
            )
        self._truncated_sessions.clear()
        for env in list(self._outbox):
            await self._send_envelope(env)

    def _ack_outbox(self, up_to_node_seq: int) -> None:
        """Drop outbox events the relay has durably stored (contiguous prefix)."""
        while self._outbox and self._outbox[0].get("node_seq", 0) <= up_to_node_seq:
            self._outbox.popleft()

    def _trim_outbox(self) -> None:
        """Bound the outbox; on overflow drop oldest and flag a resync for it."""
        while len(self._outbox) > self.settings.outbox_max:
            dropped = self._outbox.popleft()
            session_id = (dropped.get("payload") or {}).get("session_id")
            if session_id:
                self._truncated_sessions.add(session_id)

    def _is_duplicate_command(self, cmd_id: str | None) -> bool:
        """Track recently seen command ids; True if this one was already handled."""
        if not cmd_id:
            return False
        if cmd_id in self._seen_cmds:
            return True
        self._seen_cmds.add(cmd_id)
        self._seen_cmd_order.append(cmd_id)
        if len(self._seen_cmd_order) > 1000:
            self._seen_cmds.discard(self._seen_cmd_order.popleft())
        return False

    async def send_log(self, payload: dict) -> None:
        """Forward a structured log record to the relay (which ships it to CLS)."""
        await self._send_envelope(
            protocol.envelope(protocol.NODE_LOG, payload, from_node=self.node_token)
        )

    def _attach_log_forwarder(self) -> None:
        """Start streaming node logs to the relay for the life of this socket."""
        if self._log_forwarder is not None:
            return
        forwarder = logs.BridgeLogForwarder(self.send_log, self._schedule)
        logging.getLogger(logs.ROOT_LOGGER).addHandler(forwarder)
        self._log_forwarder = forwarder

    def _detach_log_forwarder(self) -> None:
        if self._log_forwarder is None:
            return
        logging.getLogger(logs.ROOT_LOGGER).removeHandler(self._log_forwarder)
        self._log_forwarder = None

    def _schedule(self, coro) -> None:
        """Run a coroutine on the daemon loop from any thread (best-effort)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro))

    async def _on_raw(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        msg_type = message.get("type")
        if msg_type == protocol.BROWSER_TO_NODE:
            # Drop a command the relay redelivers (it had queued the command
            # across our reconnect, then flushed it); running it twice would
            # re-spawn a turn or re-run a prompt.
            if self._is_duplicate_command(message.get("cmd_id")):
                return
            await self._dispatch(message.get("payload") or {})
        elif msg_type == protocol.NODE_ACK:
            up_to = (message.get("payload") or {}).get("up_to_node_seq")
            if isinstance(up_to, int):
                self._ack_outbox(up_to)
        elif msg_type == protocol.NODE_REGISTERED:
            logger.info("registered with bridge")

    async def _dispatch(self, payload: dict) -> None:
        action = payload.get("action")
        session_id = payload.get("session_id")
        trace_id = payload.get("trace_id")
        logger.info(
            "dispatch action=%s",
            action,
            extra={"trace_id": trace_id, "session_id": session_id},
        )
        try:
            if action == Action.SESSION_START:
                await self._start_session(payload)
            elif action == Action.SESSION_SEND:
                await self._send_to_session(session_id, payload)
            elif action == Action.SESSION_EDIT:
                await self._edit_session(session_id, payload)
            elif action == Action.SESSION_INTERRUPT:
                await self._interrupt_session(session_id)
            elif action == Action.SESSION_CLOSE:
                await self._close_session(session_id)
            elif action == Action.PERMISSION_RESOLVE:
                self._resolve_permission(payload)
            elif action == Action.PERMISSIONS_LIST:
                await self._send_pending_permissions()
            elif action == Action.SESSIONS_LIST:
                await self._send_snapshot()
            elif action == Action.HISTORY_LIST:
                await self._send_history_list(payload)
            elif action == Action.HISTORY_GET:
                await self._send_history_detail(payload)
            elif action == Action.FS_LIST:
                await self._send_fs_list(payload)
            elif action == Action.CAPABILITIES_GET:
                await self._send_capabilities()
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
        # Reattach when the session is already live (a resume of a session still
        # running on this node, or a follow-up): continue it instead of spawning a
        # second client over the same transcript.
        existing = self._session(session_id)
        if existing is not None:
            existing.set_trace(payload.get("trace_id"))
            await existing.send(
                payload.get("prompt", ""),
                payload.get("images"),
                payload.get("attachments"),
                **_session_overrides(payload),
            )
            return
        resume = payload.get("resume_session_id") or None
        prompt = payload.get("prompt", "")
        # Continue a Copilot session the CLI can't natively resume (e.g. a VS Code
        # Copilot Chat session opened from history): seed its transcript into a
        # fresh session instead of a doomed session/resume.
        if provider == "copilot" and resume and not history.copilot_native(resume):
            seed = await asyncio.to_thread(history.build_seed, resume)
            if seed:
                prompt = f"{seed}\n\n{prompt}".strip() if prompt else seed
            resume = None
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
            resume=resume,
            trace_id=payload.get("trace_id"),
            on_rekey=self._rekey_session,
        )
        self.sessions[session_id] = session
        await session.start(prompt, payload.get("images"), payload.get("attachments"))

    def _session(self, session_id: str | None) -> Session | None:
        """Resolve a session by its canonical id or a still-live provisional alias."""
        if not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is not None:
            return session
        alias = self._aliases.get(session_id)
        return self.sessions.get(alias) if alias else None

    def _rekey_session(self, old_id: str, new_id: str) -> None:
        """Promote a live session from its provisional id to the real (SDK) id."""
        if old_id == new_id:
            return
        session = self.sessions.pop(old_id, None)
        if session is None:
            return
        established = self.sessions.get(new_id)
        if established is not None and established is not session:
            # A session already owns the real id; never run two clients for one
            # transcript — close the just-promoted duplicate and keep the first.
            self._schedule(session.close())
            return
        self.sessions[new_id] = session
        self._aliases[old_id] = new_id

    async def _send_to_session(
        self,
        session_id: str | None,
        payload: dict,
    ) -> None:
        session = self._session(session_id)
        if session is None:
            await self.send_payload(
                event_payload(Event.SESSION_ERROR, session_id, message="unknown session")
            )
            return
        session.set_trace(payload.get("trace_id"))
        await session.send(
            payload.get("prompt", ""),
            payload.get("images"),
            payload.get("attachments"),
            **_session_overrides(payload),
        )

    async def _edit_session(self, session_id: str | None, payload: dict) -> None:
        session = self._session(session_id)
        if session is None:
            await self.send_payload(
                event_payload(Event.SESSION_ERROR, session_id, message="unknown session")
            )
            return
        session.set_trace(payload.get("trace_id"))
        await session.edit(
            payload.get("prompt", ""),
            cut_uuid=payload.get("cut_uuid") or None,
            images=payload.get("images"),
            attachments=payload.get("attachments"),
            restore_code=bool(payload.get("restore_code")),
            **_session_overrides(payload),
        )

    async def _interrupt_session(self, session_id: str | None) -> None:
        session = self._session(session_id)
        if session is not None:
            await session.interrupt()

    async def _close_session(self, session_id: str | None) -> None:
        session = self._session(session_id)
        if session is None:
            return
        # Drop by the canonical id plus any provisional alias pointing at it.
        self.sessions.pop(session.session_id, None)
        for alias, target in list(self._aliases.items()):
            if target == session.session_id or alias == session_id:
                self._aliases.pop(alias, None)
        await session.close()

    def _resolve_permission(self, payload: dict) -> None:
        request_id = payload.get("request_id")
        decision = "allow" if payload.get("decision") == "allow" else "deny"
        if not request_id:
            return
        # AskUserQuestion carries the user's structured selection alongside the
        # allow, so the provider can feed it back to the agent as the tool result
        # instead of leaving the question unanswered.
        answer = payload.get("answer")
        answer = answer if isinstance(answer, dict) else None
        for session in self.sessions.values():
            if session.resolve_permission(request_id, decision, answer):
                break

    async def _send_snapshot(self) -> None:
        await self.send_payload(
            event_payload(
                Event.SESSIONS_SNAPSHOT,
                sessions=[s.info() for s in self.sessions.values()],
            )
        )

    async def _send_pending_permissions(self) -> None:
        """Re-emit every unresolved permission request across all sessions.

        Lets a browser that (re)connects — or follows a push notification — pick
        up an approval prompt that was raised while it was away, instead of the
        request sitting blocked until it times out.
        """
        requests: list[dict] = []
        for session in self.sessions.values():
            requests.extend(session.pending_permissions())
        await self.send_payload(event_payload(Event.PERMISSIONS_SNAPSHOT, requests=requests))

    async def _send_history_list(self, payload: dict) -> None:
        limit = payload.get("limit") or 200
        sessions = await asyncio.to_thread(history.list_sessions, limit)
        # Flag transcripts whose session is actively executing a turn right now, so
        # the drawer shows a live indicator only for those. A completed session
        # stays in the registry (it's still reattachable), but it is idle, not
        # running, so it must not be flagged — that's what made every in-memory
        # session look "running". Reattach keys off registry membership separately.
        running = {sid for sid, s in self.sessions.items() if s.status == "running"}
        for summary in sessions:
            summary["running"] = summary.get("session_id") in running
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
        # The transcript carries cwd/model but never effort/permission_mode — fold
        # in the sidecar we saved while the session ran so a resume restores all of
        # them. cwd/model stay transcript-authoritative; the sidecar only backfills.
        meta = session_meta.load(self.settings.config_dir, session_id)
        if meta.get("permission_mode"):
            detail["permission_mode"] = meta["permission_mode"]
        if meta.get("effort"):
            detail["effort"] = meta["effort"]
        if not detail.get("cwd") and meta.get("cwd"):
            detail["cwd"] = meta["cwd"]
        if not detail.get("model") and meta.get("model"):
            detail["model"] = meta["model"]
        await self.send_payload(event_payload(Event.HISTORY_DETAIL, session_id, **detail))

    async def _send_fs_list(self, payload: dict) -> None:
        result = await asyncio.to_thread(
            fs.list_dir,
            payload.get("path"),
            show_hidden=bool(payload.get("show_hidden")),
        )
        await self.send_payload(event_payload(Event.FS_LIST, **result))

    async def _send_capabilities(self) -> None:
        descriptor = await capabilities.describe_detailed(self.settings)
        await self.send_payload(event_payload(Event.CAPABILITIES, **descriptor))

    async def aclose(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()


def _session_overrides(payload: dict) -> dict:
    """Live-changeable settings a follow-up turn may carry; only pass keys present."""
    return {key: payload[key] for key in ("model", "effort", "permission_mode") if key in payload}


def _is_auth_error(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if code in (401, 403):
        return True
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) in (401, 403)
