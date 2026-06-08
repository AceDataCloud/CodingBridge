"""Claude Code provider backed by ``claude-agent-sdk``.

The remote permission-relay design — forwarding the SDK's ``can_use_tool``
decision to a remote approver — follows the approach pioneered by VibeBridge.
This is an independent implementation against the public Agent SDK; no VibeBridge
source is included.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from .. import images as image_store
from ..protocol import Event, event_payload

if TYPE_CHECKING:
    from ..config import Settings
    from .base import AskPermissionFn, EmitFn

logger = logging.getLogger("coding-bridge-agent.claude")


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        session_id: str,
        emit: EmitFn,
        ask_permission: AskPermissionFn,
        settings: Settings,
    ) -> None:
        self._session_id = session_id
        self._emit = emit
        self._ask = ask_permission
        self._settings = settings
        self._cwd = settings.default_cwd
        self._client: Any = None
        self._connected = False

    async def start(
        self,
        prompt: str,
        *,
        cwd: str,
        model: str | None,
        permission_mode: str,
        effort: str | None = None,
        images: list | None = None,
        resume: str | None = None,
    ) -> None:
        self._cwd = cwd or self._settings.default_cwd
        await self._ensure_client(
            cwd=cwd, model=model, permission_mode=permission_mode, effort=effort, resume=resume
        )
        await self._turn(self._with_images(prompt, images))

    async def send(self, prompt: str, *, images: list | None = None) -> None:
        if not self._connected:
            await self._ensure_client(
                cwd=self._settings.default_cwd,
                model=self._settings.default_model,
                permission_mode="default",
            )
        await self._turn(self._with_images(prompt, images))

    async def _ensure_client(
        self,
        *,
        cwd: str,
        model: str | None,
        permission_mode: str,
        effort: str | None = None,
        resume: str | None = None,
    ) -> None:
        if self._connected:
            return
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dep
            raise RuntimeError(
                "claude-agent-sdk is not installed; run `pip install claude-agent-sdk`"
            ) from exc
        options = ClaudeAgentOptions(
            cwd=cwd or None,
            model=model,
            permission_mode=permission_mode or "default",
            can_use_tool=self._can_use_tool,
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            resume=resume or None,
        )
        _apply_effort(options, effort)
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._connected = True

    async def _turn(self, prompt: str) -> None:
        await self._client.query(prompt)
        async for message in self._client.receive_response():
            await self._handle_message(message)

    def _with_images(self, prompt: str, images: list | None) -> str:
        paths = image_store.save_images(images, self._cwd, session_id=self._session_id)
        if not paths:
            return prompt
        listing = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(paths))
        note = f"[Images provided at the following paths:]\n{listing}"
        return f"{prompt}\n\n{note}" if prompt else note

    async def _handle_message(self, message: Any) -> None:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                await self._handle_block(block)
            return
        # ResultMessage marks the end of a turn.
        if hasattr(message, "subtype") and hasattr(message, "is_error"):
            await self._emit(
                event_payload(
                    Event.SESSION_RESULT,
                    self._session_id,
                    subtype=getattr(message, "subtype", None),
                    is_error=bool(getattr(message, "is_error", False)),
                    result=getattr(message, "result", None),
                    cost_usd=getattr(message, "total_cost_usd", None),
                )
            )

    async def _handle_block(self, block: Any) -> None:
        if hasattr(block, "thinking"):
            await self._emit(
                event_payload(Event.SESSION_THINKING, self._session_id, text=block.thinking)
            )
        elif hasattr(block, "text"):
            await self._emit(event_payload(Event.SESSION_TEXT, self._session_id, text=block.text))
        elif hasattr(block, "name") and hasattr(block, "input"):
            await self._emit(
                event_payload(
                    Event.SESSION_TOOL_USE,
                    self._session_id,
                    tool=block.name,
                    tool_use_id=getattr(block, "id", None),
                    input=block.input,
                )
            )
        elif hasattr(block, "tool_use_id"):
            await self._emit(
                event_payload(
                    Event.SESSION_TOOL_RESULT,
                    self._session_id,
                    tool_use_id=block.tool_use_id,
                    content=_stringify(getattr(block, "content", None)),
                    is_error=bool(getattr(block, "is_error", False)),
                )
            )

    async def _can_use_tool(self, tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        ctx = {
            "title": getattr(context, "title", None),
            "display_name": getattr(context, "display_name", None),
            "description": getattr(context, "description", None),
        }
        decision = await self._ask(tool_name, dict(input_data or {}), ctx)
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user via Coding Bridge")

    async def interrupt(self) -> None:
        if self._client and self._connected:
            with contextlib.suppress(Exception):
                await self._client.interrupt()

    async def aclose(self) -> None:
        if self._client and self._connected:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._connected = False
        self._client = None


def _stringify(content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


_CLAUDE_EFFORT_ALIASES = {"ultra-high": "max", "ultrahigh": "max", "minimal": "low"}
_CLAUDE_EFFORT_VALUES = {"low", "medium", "high", "max"}


def _apply_effort(options: Any, effort: str | None) -> None:
    """Set the SDK reasoning effort if the installed SDK exposes the field."""
    if not effort:
        return
    value = _CLAUDE_EFFORT_ALIASES.get(effort, effort)
    if value not in _CLAUDE_EFFORT_VALUES or not hasattr(options, "effort"):
        return
    with contextlib.suppress(Exception):  # frozen-dataclass SDKs
        options.effort = value
