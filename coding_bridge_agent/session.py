"""A single coding session: wraps one provider and relays its events."""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from .permissions import PermissionBroker
from .protocol import Event, event_payload
from .providers.base import AskPermissionFn, EmitFn, ProviderFactory

# Distinguishes "caller omitted this override" from "caller cleared it to default".
_UNSET: Any = object()


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
    ) -> None:
        self.session_id = session_id
        self.status = "idle"
        self.provider = provider
        self.cwd = cwd
        self.model = model
        self.permission_mode = permission_mode
        self.effort = effort
        self.resume = resume
        self._emit = emit
        self._settings = settings
        self._broker = PermissionBroker()
        ask: AskPermissionFn = self._ask_permission
        self._provider = provider_factory(provider, session_id, emit, ask)
        self._task: asyncio.Task[None] | None = None

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
            self._provider.start(
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
            self._provider.send(
                prompt,
                images=images,
                attachments=attachments,
                model=self.model,
                effort=self.effort,
                permission_mode=self.permission_mode,
            )
        )

    def _spawn(self, coro: Any) -> None:
        if self._task and not self._task.done():
            # A turn is already running; chain after it so inputs stay ordered.
            self._task = asyncio.create_task(self._chain(self._task, coro))
            return
        self._task = asyncio.create_task(self._guard(coro))

    async def _chain(self, previous: asyncio.Task[None], coro: Any) -> None:
        with contextlib.suppress(Exception):
            await previous
        await self._guard(coro)

    async def _guard(self, coro: Any) -> None:
        self.status = "running"
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface to the browser, keep node alive
            await self._emit(event_payload(Event.SESSION_ERROR, self.session_id, message=str(exc)))
        finally:
            self.status = "idle"

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
