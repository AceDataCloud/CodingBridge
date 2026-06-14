"""Provider abstraction — one coding agent backend per session."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..permissions import Resolution


def slash_name(prompt: str) -> str | None:
    """Extract a slash-command name, or None if the prompt isn't a command.

    A command is a leading ``/`` followed by a single token with no further
    ``/`` (so filesystem-like text such as ``/Users/x`` stays a normal prompt).
    """
    text = (prompt or "").strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    first = text.split()[0]
    body = first[1:]
    if not body or "/" in body:
        return None
    return body.lower()


# Sends an inner event payload toward the browser.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
# (tool_name, tool_input, context) -> Resolution(decision, answer). The answer
# is the structured reply for interactive tools (AskUserQuestion); None for a
# plain allow/deny gate.
AskPermissionFn = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable["Resolution"]]


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

    async def edit(
        self,
        prompt: str,
        *,
        cut_uuid: str | None,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        images: list | None = None,
        attachments: list | None = None,
        restore_code: bool = False,
    ) -> None: ...

    async def interrupt(self) -> None: ...

    async def aclose(self) -> None: ...


# (provider_name, session_id, emit, ask_permission) -> Provider.
ProviderFactory = Callable[[str, str, EmitFn, AskPermissionFn], Provider]
