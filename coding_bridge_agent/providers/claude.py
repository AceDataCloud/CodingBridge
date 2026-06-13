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

from .. import attachments as attachment_store
from .. import capabilities
from .. import images as image_store
from ..protocol import Event, event_payload
from .base import slash_name

if TYPE_CHECKING:
    from ..config import Settings
    from .base import AskPermissionFn, EmitFn

logger = logging.getLogger("coding-bridge-agent.claude")

# Phrase the claude CLI returns for TUI-only commands it can't run headlessly.
_UNAVAILABLE_SUFFIX = "isn't available in this environment."


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
        self._model: str | None = settings.default_model
        self._permission_mode = "default"
        self._client: Any = None
        self._connected = False
        self._server_info: dict[str, Any] | None = None
        self._known_commands: set[str] = set()
        # Per-turn partial-message (token) streaming state.
        self._turn_seq = 0
        self._stream_text_ord = 0
        self._open_text: dict[Any, dict[str, Any]] = {}
        self._saw_text_stream = False
        # Resume-replay guard (first resumed turn only); see _gated_receive.
        self._gate_active = False
        self._gate_uuids: set[str] = set()
        self._gate_msg_ids: set[str] = set()
        self._gate_stream_replay = False
        self._gate_saw_replay = False
        self._gate_saw_genuine = False
        self._gate_swallowed_result = False

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
    ) -> None:
        self._cwd = cwd or self._settings.default_cwd
        self._model = model
        self._permission_mode = permission_mode or "default"
        self._arm_resume_guard(resume)
        await self._ensure_client(
            cwd=cwd, model=model, permission_mode=permission_mode, effort=effort, resume=resume
        )
        if await self._maybe_handle_slash(prompt):
            return
        await self._turn(self._with_attachments(prompt, images, attachments))

    def _arm_resume_guard(self, resume: str | None) -> None:
        """Load the resumed transcript's ids so the first turn can drop a replay."""
        self._gate_active = False
        self._gate_uuids = set()
        self._gate_msg_ids = set()
        if not resume:
            return
        from .. import history

        with contextlib.suppress(Exception):
            self._gate_uuids, self._gate_msg_ids = history.claude_known_ids(resume)
        self._gate_active = bool(self._gate_uuids or self._gate_msg_ids)

    async def send(
        self,
        prompt: str,
        *,
        images: list | None = None,
        attachments: list | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
    ) -> None:
        if not self._connected:
            await self._ensure_client(
                cwd=self._settings.default_cwd,
                model=model if model is not None else self._settings.default_model,
                permission_mode=permission_mode or "default",
                effort=effort,
            )
        else:
            await self._apply_runtime_changes(model, permission_mode)
        if await self._maybe_handle_slash(prompt):
            return
        await self._turn(self._with_attachments(prompt, images, attachments))

    async def _apply_runtime_changes(
        self, model: str | None, permission_mode: str | None
    ) -> None:
        """Apply the mid-session changes the streaming SDK supports live.

        Reasoning effort has no live SDK setter, so it only takes effect on the
        next fresh session; model and permission mode switch in place.
        """
        if model != self._model and hasattr(self._client, "set_model"):
            with contextlib.suppress(Exception):
                await self._client.set_model(model or None)
            self._model = model
        if (
            permission_mode
            and permission_mode != self._permission_mode
            and hasattr(self._client, "set_permission_mode")
        ):
            with contextlib.suppress(Exception):
                await self._client.set_permission_mode(permission_mode)
            self._permission_mode = permission_mode

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
        # Stream assistant text token-by-token when the SDK supports it.
        if hasattr(options, "include_partial_messages"):
            options.include_partial_messages = True
        _apply_effort(options, effort)
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._connected = True
        self._model = model
        self._permission_mode = permission_mode or "default"
        await self._load_server_info()

    async def _load_server_info(self) -> None:
        """Cache the initialize result so we know which slash commands run here."""
        with contextlib.suppress(Exception):
            self._server_info = await self._client.get_server_info()
        self._known_commands = capabilities.command_name_set(
            capabilities.normalize_commands(self._server_info)
        )

    async def _maybe_handle_slash(self, prompt: str) -> bool:
        """Short-circuit slash commands the SDK can't run headlessly.

        Returns True when the command was handled locally (a synthesized answer
        or a localized notice), so the caller skips the normal SDK turn. With no
        catalog yet we defer to the SDK and rely on the runtime fallback.
        """
        if not self._known_commands:
            return False
        name = slash_name(prompt)
        if name is None or name in self._known_commands:
            return False
        if name == "status":
            await self._emit_status()
        else:
            await self._emit_slash_notice(name)
        await self._emit_result_done("notice")
        return True

    async def _turn(self, prompt: str) -> None:
        self._begin_stream_turn()
        await self._client.query(prompt)
        if self._gate_active:
            await self._gated_receive()
        else:
            async for message in self._client.receive_response():
                await self._handle_message(message)
        await self._flush_open_text()

    async def _gated_receive(self) -> None:
        """First resumed turn: drop any transcript the CLI replays verbatim.

        Some claude CLI versions re-stream the whole resumed conversation (ending
        in its own result) before processing the new turn. Those replayed messages
        reuse the transcript's original ids, so we drop them — and swallow the
        replay's result instead of letting it end the turn — and forward only the
        genuinely new output. A no-op on CLIs that don't replay (no id matches).
        """
        self._gate_stream_replay = False
        self._gate_saw_replay = False
        self._gate_saw_genuine = False
        self._gate_swallowed_result = False
        try:
            async for message in self._client.receive_messages():
                if await self._gated_handle(message):
                    break
        finally:
            self._gate_active = False

    async def _gated_handle(self, message: Any) -> bool:
        """Filter one message during the resumed first turn; True ends the turn."""
        stream_event = getattr(message, "event", None)
        if isinstance(stream_event, dict):
            if stream_event.get("type") == "message_start":
                msg = stream_event.get("message") or {}
                mid = msg.get("id")
                self._gate_stream_replay = bool(mid and mid in self._gate_msg_ids)
                self._gate_saw_replay = self._gate_saw_replay or self._gate_stream_replay
                return False
            if self._gate_stream_replay:
                return False  # drop deltas/stop of a replayed streaming message
            self._gate_saw_genuine = True
            await self._handle_stream_event(stream_event)
            return False
        content = getattr(message, "content", None)
        if isinstance(content, list):
            uid = getattr(message, "uuid", None)
            if uid and uid in self._gate_uuids:
                self._gate_saw_replay = True
                return False  # drop a replayed complete message
            self._gate_saw_genuine = True
            for block in content:
                await self._handle_block(block)
            return False
        # ResultMessage: swallow the replay's terminating result once, so the
        # genuine turn that follows it still streams; otherwise end the turn.
        if hasattr(message, "subtype") and hasattr(message, "is_error"):
            replay_result = (
                self._gate_saw_replay
                and not self._gate_saw_genuine
                and not self._gate_swallowed_result
            )
            if replay_result:
                self._gate_swallowed_result = True
                return False
            await self._handle_message(message)
            return True
        self._note_system(message)
        return False

    def _note_system(self, message: Any) -> None:
        """Log the resumed node's CLI version (rides node.log → CLS) once."""
        if getattr(message, "subtype", None) != "init":
            return
        data = getattr(message, "data", None)
        version = data.get("claude_code_version") if isinstance(data, dict) else None
        if version:
            logger.info(
                "claude resume init version=%s session=%s", version, self._session_id
            )

    def _with_attachments(
        self, prompt: str, images: list | None, attachments: list | None
    ) -> str:
        image_paths = image_store.save_images(images, self._cwd, session_id=self._session_id)
        files = attachment_store.save_attachments(
            attachments, self._cwd, session_id=self._session_id
        )
        return attachment_store.attachment_note(prompt, files, image_paths)

    async def _handle_message(self, message: Any) -> None:
        stream_event = getattr(message, "event", None)
        if isinstance(stream_event, dict):
            await self._handle_stream_event(stream_event)
            return
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                await self._handle_block(block)
            return
        # ResultMessage marks the end of a turn.
        if hasattr(message, "subtype") and hasattr(message, "is_error"):
            await self._flush_open_text()
            result = getattr(message, "result", None)
            # Safety net: an unknown TUI-only command slipped past the catalog
            # check. Swap the cryptic CLI string for a localized notice.
            if isinstance(result, str) and result.rstrip().endswith(_UNAVAILABLE_SUFFIX):
                await self._emit_slash_notice(_command_from_rejection(result))
                result = None
            await self._emit(
                event_payload(
                    Event.SESSION_RESULT,
                    self._session_id,
                    subtype=getattr(message, "subtype", None),
                    is_error=bool(getattr(message, "is_error", False)),
                    result=result,
                    cost_usd=getattr(message, "total_cost_usd", None),
                )
            )

    async def _emit_status(self) -> None:
        """Synthesize a `/status` answer from the cached initialize info + session."""
        info = self._server_info or {}
        account = info.get("account") or {}
        lines = ["## Status", ""]
        email = account.get("email")
        if email:
            org = account.get("organization")
            lines.append(f"- **Account:** {email} ({org})" if org else f"- **Account:** {email}")
        subscription = account.get("subscriptionType")
        if subscription:
            lines.append(f"- **Subscription:** {subscription}")
        if self._model:
            lines.append(f"- **Model:** {self._model}")
        lines.append(f"- **Permission mode:** {self._permission_mode}")
        lines.append(f"- **Working directory:** {self._cwd or self._settings.default_cwd}")
        await self._emit(
            event_payload(Event.SESSION_TEXT, self._session_id, text="\n".join(lines))
        )

    async def _emit_slash_notice(self, name: str) -> None:
        await self._emit(
            event_payload(
                Event.SESSION_NOTICE,
                self._session_id,
                level="info",
                code="slash_unavailable",
                command=name,
                text=(
                    f"/{name} is an interactive command and can't run in a remote "
                    "Coding Bridge session."
                ),
            )
        )

    async def _emit_result_done(self, subtype: str) -> None:
        await self._emit(
            event_payload(
                Event.SESSION_RESULT,
                self._session_id,
                subtype=subtype,
                is_error=False,
                result=None,
            )
        )

    def _begin_stream_turn(self) -> None:
        """Reset per-turn streaming state before a new query."""
        self._turn_seq += 1
        self._stream_text_ord = 0
        self._open_text = {}
        self._saw_text_stream = False

    async def _flush_open_text(self) -> None:
        """Commit any streamed text block that never saw a stop event."""
        if not self._open_text:
            return
        for blk in list(self._open_text.values()):
            await self._emit(
                event_payload(
                    Event.SESSION_TEXT, self._session_id, text=blk["text"], id=blk["id"]
                )
            )
        self._open_text = {}

    async def _handle_stream_event(self, raw: dict[str, Any]) -> None:
        """Relay Anthropic partial-message events as incremental text deltas.

        Only assistant text streams token-by-token; thinking and tool blocks are
        still emitted whole from the assembled AssistantMessage.
        """
        etype = raw.get("type")
        if etype == "content_block_start":
            block = raw.get("content_block") or {}
            if block.get("type") == "text":
                stream_id = f"{self._session_id}:{self._turn_seq}:{self._stream_text_ord}"
                self._stream_text_ord += 1
                self._open_text[raw.get("index")] = {"id": stream_id, "text": ""}
        elif etype == "content_block_delta":
            delta = raw.get("delta") or {}
            if delta.get("type") == "text_delta":
                blk = self._open_text.get(raw.get("index"))
                if blk is not None:
                    chunk = delta.get("text") or ""
                    blk["text"] += chunk
                    self._saw_text_stream = True
                    await self._emit(
                        event_payload(
                            Event.SESSION_TEXT_DELTA,
                            self._session_id,
                            text=chunk,
                            id=blk["id"],
                        )
                    )
        elif etype == "content_block_stop":
            blk = self._open_text.pop(raw.get("index"), None)
            if blk is not None:
                await self._emit(
                    event_payload(
                        Event.SESSION_TEXT, self._session_id, text=blk["text"], id=blk["id"]
                    )
                )

    async def _handle_block(self, block: Any) -> None:
        if hasattr(block, "thinking"):
            await self._emit(
                event_payload(Event.SESSION_THINKING, self._session_id, text=block.thinking)
            )
        elif hasattr(block, "text"):
            # Already streamed + committed via stream events this turn.
            if self._saw_text_stream:
                return
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


def _command_from_rejection(result: str) -> str:
    """Pull the command name out of '<cmd> isn't available in this environment.'."""
    token = result.strip().split(" ", 1)[0]
    return token.lstrip("/") or "command"


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
