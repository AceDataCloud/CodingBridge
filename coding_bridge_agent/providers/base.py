"""Provider abstraction — one coding agent backend per session."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

# Sends an inner event payload toward the browser.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
# (tool_name, tool_input, context) -> "allow" | "deny".
AskPermissionFn = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]


class Provider(Protocol):
    """A coding agent backend bound to a single session."""

    name: str

    async def start(
        self,
        prompt: str,
        *,
        cwd: str,
        model: str | None,
        permission_mode: str,
        effort: str | None = None,
        images: list | None = None,
        attachments: list | None = None,
        resume: str | None = None,
    ) -> None: ...

    async def send(
        self,
        prompt: str,
        *,
        images: list | None = None,
        attachments: list | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
    ) -> None: ...

    async def interrupt(self) -> None: ...

    async def aclose(self) -> None: ...


# (provider_name, session_id, emit, ask_permission) -> Provider.
ProviderFactory = Callable[[str, str, EmitFn, AskPermissionFn], Provider]
