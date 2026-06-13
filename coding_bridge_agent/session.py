"""A single coding session: wraps one provider and relays its events."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .permissions import PermissionBroker
from .protocol import Event, event_payload
from .providers.base import AskPermissionFn, EmitFn, ProviderFactory

logger = logging.getLogger("coding-bridge-agent.session")

# Distinguishes "caller omitted this override" from "caller cleared it to default".
_UNSET: Any = object()

# A fresh coroutine factory so a turn can be re-awaited on retry.
_TurnFactory = Callable[[], Awaitable[None]]


def _classify_error(exc: Exception) -> dict[str, Any]:
    """Turn a provider exception into a structured, browser-friendly descriptor.

    ``code`` drives the frontend's localized message; ``fatal`` means the
    provider subprocess is dead and must be reset; ``retryable`` means the turn
    crashed at the transport level (e.g. the claude CLI segfaulted on spawn) and
    is worth retrying once.
    """
    name = type(exc).__name__
    exit_code = getattr(exc, "exit_code", None)
    stderr = getattr(exc, "stderr", None)
    if name == "ProcessError" or exit_code is not None:
        code, fatal, retryable = "process_crashed", True, True
    elif name in {"CLIConnectionError", "CLIJSONDecodeError"}:
        code, fatal, retryable = "transport_error", True, True
    elif name == "CLINotFoundError":
        code, fatal, retryable = "cli_not_found", True, False
    else:
        code, fatal, retryable = "provider_error", False, False
    return {
        "code": code,
        "exception": name,
        "exit_code": exit_code,
        "stderr": stderr or None,
        "fatal": fatal,
        "retryable": retryable,
    }


class Session:
    def __init__(
        self,
        session_id: str,
        provider_factory: ProviderFactory,
        emit: EmitFn,
        settings: Any,
        *,
        cwd: str,
        model: str | None,
        permission_mode: str,
        provider: str = "claude",
        effort: str | None = None,
        resume: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.status = "idle"
        self.provider = provider
        self.cwd = cwd
        self.model = model
        self.permission_mode = permission_mode
        self.effort = effort
        self.resume = resume
        self.trace_id = trace_id
        self._raw_emit = emit
        self._settings = settings
        self._broker = PermissionBroker()
        ask: AskPermissionFn = self._ask_permission
        self._provider = provider_factory(provider, session_id, self._emit, ask)
        self._task: asyncio.Task[None] | None = None
        # Set by _emit; lets _guard tell a no-op crash (safe to retry) from one
        # that already streamed output or ran a tool (unsafe to replay).
        self._turn_emitted = False

    def set_trace(self, trace_id: str | None) -> None:
        """Adopt the trace id carried by the latest turn (if any)."""
        if trace_id:
            self.trace_id = trace_id

    async def _emit(self, payload: dict[str, Any]) -> None:
        """Stamp the active trace id onto every outgoing event, then forward."""
        self._turn_emitted = True
        if self.trace_id and "trace_id" not in payload:
            payload = {**payload, "trace_id": self.trace_id}
        await self._raw_emit(payload)

    async def _ask_permission(
        self, tool_name: str, input_data: dict[str, Any], ctx: dict[str, Any]
    ) -> str:
        request_id = uuid.uuid4().hex
        await self._emit(
            event_payload(
                Event.PERMISSION_REQUEST,
                self.session_id,
                request_id=request_id,
                tool=tool_name,
                input=input_data,
                title=ctx.get("title"),
                display_name=ctx.get("display_name"),
                description=ctx.get("description"),
            )
        )
        return await self._broker.request(request_id, self._settings.permission_timeout)

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        return self._broker.resolve(request_id, "allow" if decision == "allow" else "deny")

    async def start(
        self, prompt: str, images: list | None = None, attachments: list | None = None
    ) -> None:
        await self._emit(
            event_payload(
                Event.SESSION_STARTED,
                self.session_id,
                cwd=self.cwd,
                model=self.model,
                provider=self.provider,
            )
        )
        self._spawn(
            lambda: self._provider.start(
                prompt,
                cwd=self.cwd,
                model=self.model,
                permission_mode=self.permission_mode,
                effort=self.effort,
                images=images,
                attachments=attachments,
                resume=self.resume,
            )
        )

    async def send(
        self,
        prompt: str,
        images: list | None = None,
        attachments: list | None = None,
        *,
        model: Any = _UNSET,
        effort: Any = _UNSET,
        permission_mode: Any = _UNSET,
    ) -> None:
        # A follow-up turn may carry new settings; remember them so info()/snapshots
        # and later turns reflect the change.
        if model is not _UNSET:
            self.model = model or None
        if effort is not _UNSET:
            self.effort = effort or None
        if permission_mode is not _UNSET and permission_mode:
            self.permission_mode = permission_mode
        self._spawn(
            lambda: self._provider.send(
                prompt,
                images=images,
                attachments=attachments,
                model=self.model,
                effort=self.effort,
                permission_mode=self.permission_mode,
            )
        )

    async def edit(
        self,
        prompt: str,
        *,
        cut_uuid: str | None,
        images: list | None = None,
        attachments: list | None = None,
        model: Any = _UNSET,
        effort: Any = _UNSET,
        permission_mode: Any = _UNSET,
        restore_code: bool = False,
    ) -> None:
        # Edit re-runs the turn from a fork point; remember any new settings the
        # same way send() does so info()/snapshots and later turns reflect them.
        if model is not _UNSET:
            self.model = model or None
        if effort is not _UNSET:
            self.effort = effort or None
        if permission_mode is not _UNSET and permission_mode:
            self.permission_mode = permission_mode
        # Announce the fork as a sequenced, logged event so a browser that
        # reconnects replays it and truncates its view to the cut point — instead
        # of replaying the abandoned turns. `cut_uuid` matches the result event
        # the browser rendered for the kept turn; `prompt` lets it re-show the
        # edited user message after a history-less replay.
        await self._emit(
            event_payload(
                Event.SESSION_REWOUND,
                self.session_id,
                cut_uuid=cut_uuid,
                prompt=prompt,
            )
        )
        self._spawn(
            lambda: self._provider.edit(
                prompt,
                cut_uuid=cut_uuid,
                model=self.model,
                permission_mode=self.permission_mode,
                effort=self.effort,
                images=images,
                attachments=attachments,
                restore_code=restore_code,
            )
        )

    def _spawn(self, make_turn: _TurnFactory) -> None:
        if self._task and not self._task.done():
            # A turn is already running; chain after it so inputs stay ordered.
            self._task = asyncio.create_task(self._chain(self._task, make_turn))
            return
        self._task = asyncio.create_task(self._guard(make_turn))

    async def _chain(self, previous: asyncio.Task[None], make_turn: _TurnFactory) -> None:
        with contextlib.suppress(Exception):
            await previous
        await self._guard(make_turn)

    async def _guard(self, make_turn: _TurnFactory) -> None:
        self.status = "running"
        attempt = 0
        try:
            while True:
                self._turn_emitted = False
                try:
                    await make_turn()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - surface to browser, keep node alive
                    info = _classify_error(exc)
                    # Only retry a clean crash: transport-level failure that hadn't
                    # streamed anything yet, so replaying can't duplicate effects.
                    if (
                        info["retryable"]
                        and not self._turn_emitted
                        and attempt < self._settings.turn_retry_limit
                    ):
                        attempt += 1
                        logger.warning(
                            "session %s turn crashed (%s exit_code=%s); retry %d/%d",
                            self.session_id, info["exception"], info["exit_code"],
                            attempt, self._settings.turn_retry_limit,
                        )
                        await self._reset_provider()
                        await asyncio.sleep(self._settings.turn_retry_backoff)
                        continue
                    await self._fail(exc, info)
                    return
        finally:
            self.status = "idle"

    async def _fail(self, exc: Exception, info: dict[str, Any]) -> None:
        """Log the crash (rides node.log → CLS) and report it to the browser."""
        logger.exception(
            "session %s turn failed: code=%s exception=%s exit_code=%s cwd=%s model=%s",
            self.session_id, info["code"], info["exception"], info["exit_code"],
            self.cwd, self.model,
        )
        await self._emit(
            event_payload(
                Event.SESSION_ERROR,
                self.session_id,
                message=str(exc),
                code=info["code"],
                exception=info["exception"],
                exit_code=info["exit_code"],
                stderr=info["stderr"],
            )
        )
        # A dead subprocess leaves the provider unusable; reset so the next turn
        # reconnects instead of operating on a stale client.
        if info["fatal"]:
            await self._reset_provider()

    async def _reset_provider(self) -> None:
        with contextlib.suppress(Exception):
            await self._provider.aclose()

    async def interrupt(self) -> None:
        await self._provider.interrupt()

    async def close(self) -> None:
        self._broker.cancel_all("deny")
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._provider.aclose()
        await self._emit(event_payload(Event.SESSION_CLOSED, self.session_id))

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "provider": self.provider,
            "cwd": self.cwd,
            "model": self.model,
        }
