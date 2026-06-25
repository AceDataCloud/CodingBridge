"""GitHub Copilot CLI provider backed by its ACP (Agent Client Protocol) server.

Runs ``copilot --acp --stdio`` as a subprocess and speaks ACP — JSON-RPC 2.0 over
NDJSON on stdio — to drive a live session. The agent's ``session/update``
notifications are relayed as the same inner events the Claude and Codex providers
emit, and its ``session/request_permission`` calls are routed through the same
remote approval relay (``ask_permission``) as the other backends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from .. import attachments as attachment_store
from .. import capabilities
from .. import images as image_store
from ..protocol import Event, event_payload

if TYPE_CHECKING:
    from ..config import Settings
    from .base import AskPermissionFn, EmitFn

logger = logging.getLogger("coding-bridge.copilot")

ACP_PROTOCOL_VERSION = 1
# Only bypassPermissions auto-approves every tool; every other mode keeps the
# remote approval relay on (permission relay is always on by default, by design).
_AUTO_APPROVE_MODES = {"bypassPermissions"}
_EFFORT_ALIASES = {"max": "high", "ultra-high": "high", "ultrahigh": "high", "minimal": "low"}
_EFFORT_VALUES = {"low", "medium", "high"}


def _copilot_effort(effort: str | None) -> str | None:
    if not effort:
        return None
    value = _EFFORT_ALIASES.get(effort, effort)
    return value if value in _EFFORT_VALUES else None


class _AcpError(Exception):
    """A JSON-RPC error returned by the Copilot ACP agent."""

    def __init__(self, error: Any) -> None:
        self.code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error)
        super().__init__(message or "ACP error")


class CopilotProvider:
    name = "copilot"

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
        self._effort: str | None = None
        self._permission_mode = "default"
        # ACP transport + handshake state.
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []
        self._connected = False
        self._acp_session_id: str | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._aux_tasks: set[asyncio.Task[None]] = set()
        self._caps_load = False
        self._caps_resume = False
        self._caps_close = False
        self._announced_identity = False
        # Per-turn streaming state for assembling chunked message/thought text.
        self._turn_seq = 0
        self._stream_ord = 0
        self._cur: dict[str, Any] | None = None
        self._usage: dict[str, Any] | None = None

    # --- Provider protocol -------------------------------------------------
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
        self._effort = _copilot_effort(effort)
        await self._ensure_proc(resume=resume)
        await self._turn(self._with_attachments(prompt, images, attachments))

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
        if permission_mode:
            self._permission_mode = permission_mode
        # The ACP model/effort is fixed per process, so a mid-session change is
        # remembered and only takes effect on the next fresh connection.
        if model is not None:
            self._model = model
        if effort is not None:
            self._effort = _copilot_effort(effort)
        await self._ensure_proc(resume=self._acp_session_id)
        await self._turn(self._with_attachments(prompt, images, attachments))

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
    ) -> None:
        """Editing is unsupported for Copilot — ACP has no fork/truncate primitive.

        Capabilities advertise this off, so this is only a defensive guard;
        report it cleanly instead of forking blindly.
        """
        await self._emit(
            event_payload(
                Event.SESSION_NOTICE,
                self._session_id,
                level="info",
                code="edit_unsupported",
                text="Editing a past prompt isn't supported for Copilot sessions.",
            )
        )
        await self._emit(
            event_payload(
                Event.SESSION_RESULT,
                self._session_id,
                subtype="notice",
                is_error=False,
                usage=None,
            )
        )

    async def interrupt(self) -> None:
        if self._connected and self._acp_session_id:
            with contextlib.suppress(Exception):
                await self._notify("session/cancel", {"sessionId": self._acp_session_id})

    async def aclose(self) -> None:
        if self._connected and self._acp_session_id and self._caps_close:
            with contextlib.suppress(Exception):
                await self._request("session/close", {"sessionId": self._acp_session_id})
        self._connected = False
        for task in list(self._aux_tasks):
            task.cancel()
        self._aux_tasks.clear()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        self._fail_pending(RuntimeError("copilot session closed"))

    # --- connection lifecycle ---------------------------------------------
    async def _ensure_proc(self, *, resume: str | None) -> None:
        if self._connected and self._proc is not None and self._proc.returncode is None:
            return
        # A previous process died between turns; tear it down before reconnecting
        # so the next turn re-spawns instead of writing into a dead pipe and
        # parking on a future that never resolves.
        if self._proc is not None or self._reader_task is not None:
            await self.aclose()
        await self._spawn_and_connect(resume=resume)

    async def _spawn_and_connect(self, *, resume: str | None) -> None:
        cli = capabilities.resolve_cli("copilot", self._settings)
        if cli is None:
            raise RuntimeError(
                "GitHub Copilot CLI is not installed; install it from "
                "https://github.com/github/copilot-cli"
            )
        argv = [cli, "--acp", "--stdio"]
        if self._effort:
            argv += ["--reasoning-effort", self._effort]
        env = dict(os.environ)
        if self._model:
            env["COPILOT_MODEL"] = self._model
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._cwd or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._stderr_tail = []
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._initialize()
            await self._open_session(resume=resume)
        except BaseException:
            await self.aclose()
            raise
        self._connected = True

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {"protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}},
        )
        caps = result.get("agentCapabilities") or {} if isinstance(result, dict) else {}
        self._caps_load = bool(caps.get("loadSession"))
        session_caps = caps.get("sessionCapabilities") or {}
        self._caps_resume = "resume" in session_caps
        self._caps_close = "close" in session_caps

    async def _open_session(self, *, resume: str | None) -> None:
        params = {"cwd": os.path.abspath(self._cwd or "."), "mcpServers": []}
        if resume and self._caps_resume:
            await self._request("session/resume", {**params, "sessionId": resume})
            self._acp_session_id = resume
        elif resume and self._caps_load:
            await self._request("session/load", {**params, "sessionId": resume})
            self._acp_session_id = resume
        else:
            result = await self._request("session/new", params)
            self._acp_session_id = result.get("sessionId") if isinstance(result, dict) else None
        await self._maybe_announce_identity()

    async def _maybe_announce_identity(self) -> None:
        """Adopt the ACP session id as this session's canonical id, once known.

        The browser opens under a provisional id; the agent assigns the real id
        at ``session/new``. We emit session.identified and re-tag later events so
        the live session and its history entry share one identity. A resume opens
        under the real id already, so this is a no-op there.
        """
        sid = self._acp_session_id
        if self._announced_identity or not sid:
            return
        self._announced_identity = True
        if sid == self._session_id:
            return
        old = self._session_id
        await self._emit(event_payload(Event.SESSION_IDENTIFIED, old, sdk_session_id=sid))
        self._session_id = sid

    # --- a single prompt turn ---------------------------------------------
    async def _turn(self, prompt: str) -> None:
        self._turn_seq += 1
        self._stream_ord = 0
        self._cur = None
        self._usage = None
        blocks = [{"type": "text", "text": prompt}]
        try:
            result = await self._request(
                "session/prompt", {"sessionId": self._acp_session_id, "prompt": blocks}
            )
        except _AcpError as exc:
            await self._flush_cur()
            await self._emit(
                event_payload(Event.SESSION_ERROR, self._session_id, message=str(exc))
            )
            return
        await self._flush_cur()
        stop = result.get("stopReason") if isinstance(result, dict) else None
        await self._emit(
            event_payload(
                Event.SESSION_RESULT,
                self._session_id,
                subtype=stop or "end_turn",
                is_error=stop == "refusal",
                usage=self._usage,
            )
        )

    def _with_attachments(
        self, prompt: str, images: list | None, attachments: list | None
    ) -> str:
        image_paths = image_store.save_images(images, self._cwd, session_id=self._session_id)
        files = attachment_store.save_attachments(
            attachments, self._cwd, session_id=self._session_id
        )
        return attachment_store.attachment_note(prompt, files, image_paths)

    # --- session/update event mapping (relay-facing) ----------------------
    async def _handle_update(self, params: dict[str, Any]) -> None:
        """Map one ACP ``session/update`` notification to inner browser events."""
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            await self._stream_chunk("text", update)
        elif kind == "agent_thought_chunk":
            await self._stream_chunk("thought", update)
        elif kind == "tool_call":
            await self._flush_cur()
            await self._emit_tool_call(update)
        elif kind == "tool_call_update":
            await self._emit_tool_update(update)
        elif kind == "usage_update":
            self._usage = {
                "used": update.get("used"),
                "size": update.get("size"),
                "cost": update.get("cost"),
            }
        # plan / available_commands_update / user_message_chunk (load replay): ignored.

    async def _stream_chunk(self, kind: str, update: dict[str, Any]) -> None:
        text = _content_text(update.get("content"))
        if not text:
            return
        msg_id = update.get("messageId")
        cur = self._cur
        if cur and (cur["kind"] != kind or cur["msg_id"] != msg_id):
            await self._flush_cur()
            cur = None
        if cur is None:
            self._stream_ord += 1
            stream_id = msg_id or f"{self._session_id}:{self._turn_seq}:{self._stream_ord}"
            cur = self._cur = {"kind": kind, "id": stream_id, "msg_id": msg_id, "text": ""}
        cur["text"] += text
        # Only assistant text streams token-by-token; thought is committed whole.
        if kind == "text":
            await self._emit(
                event_payload(
                    Event.SESSION_TEXT_DELTA, self._session_id, text=text, id=cur["id"]
                )
            )

    async def _flush_cur(self) -> None:
        """Commit the open streaming block (text or thought), if any."""
        cur = self._cur
        self._cur = None
        if not cur or not cur["text"]:
            return
        if cur["kind"] == "text":
            await self._emit(
                event_payload(
                    Event.SESSION_TEXT, self._session_id, text=cur["text"], id=cur["id"]
                )
            )
        else:
            await self._emit(
                event_payload(Event.SESSION_THINKING, self._session_id, text=cur["text"])
            )

    async def _emit_tool_call(self, update: dict[str, Any]) -> None:
        await self._emit(
            event_payload(
                Event.SESSION_TOOL_USE,
                self._session_id,
                tool=update.get("title") or update.get("kind") or "tool",
                tool_use_id=update.get("toolCallId"),
                input=update.get("rawInput") or {},
            )
        )

    async def _emit_tool_update(self, update: dict[str, Any]) -> None:
        status = update.get("status")
        if status not in ("completed", "failed"):
            return  # pending / in_progress carry no terminal result yet
        await self._flush_cur()
        await self._emit(
            event_payload(
                Event.SESSION_TOOL_RESULT,
                self._session_id,
                tool_use_id=update.get("toolCallId"),
                content=_tool_content_text(update.get("content")),
                is_error=status == "failed",
            )
        )

    # --- permission relay --------------------------------------------------
    async def _resolve_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        """Turn an ACP ``session/request_permission`` into an outcome.

        bypassPermissions auto-selects an allow option; every other mode forwards
        the request to the remote approver and maps the verdict to an option.
        """
        options = params.get("options") or []
        tool_call = params.get("toolCall") or {}
        if self._permission_mode in _AUTO_APPROVE_MODES:
            return self._outcome(_select_option(options, allow=True))
        tool_name = tool_call.get("title") or tool_call.get("kind") or "tool"
        ctx = {
            "title": tool_call.get("title"),
            "display_name": None,
            "description": _tool_content_text(tool_call.get("content")),
        }
        resolution = await self._ask(tool_name, dict(tool_call.get("rawInput") or {}), ctx)
        allow = resolution.decision == "allow"
        return self._outcome(_select_option(options, allow=allow))

    @staticmethod
    def _outcome(option_id: str | None) -> dict[str, Any]:
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def _serve_permission(self, request_id: int, params: dict[str, Any]) -> None:
        try:
            result = await self._resolve_permission(params)
        except Exception:  # noqa: BLE001 - a failed approval must not wedge the agent
            result = {"outcome": {"outcome": "cancelled"}}
        with contextlib.suppress(Exception):
            await self._respond(request_id, result)

    # --- JSON-RPC transport ------------------------------------------------
    async def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            # Mark the transport dead so the next turn reconnects instead of
            # reusing a closed pipe, and fail any in-flight request so its caller
            # raises (and session.py resets us) rather than hanging forever.
            self._connected = False
            self._fail_pending(RuntimeError("copilot ACP connection closed"))

    async def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:
                await self._on_agent_request(msg)
            else:
                await self._on_agent_notification(msg)
            return
        future = self._pending.pop(msg.get("id"), None)
        if future is None or future.done():
            return
        error = msg.get("error")
        if error is not None:
            future.set_exception(_AcpError(error))
        else:
            future.set_result(msg.get("result"))

    async def _on_agent_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        request_id = msg.get("id")
        if method == "session/request_permission":
            # The approver may take minutes; serve it off the reader loop so
            # session/update notifications keep flowing meanwhile.
            task = asyncio.create_task(self._serve_permission(request_id, msg.get("params") or {}))
            self._aux_tasks.add(task)
            task.add_done_callback(self._aux_tasks.discard)
        else:
            with contextlib.suppress(Exception):
                await self._respond_error(request_id, -32601, f"method not supported: {method}")

    async def _on_agent_notification(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "session/update":
            await self._handle_update(msg.get("params") or {})

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._write(message)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return await future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _write(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("copilot ACP process is not running")
        proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        # Let a dead-pipe drain raise so a turn fails loudly (and session.py resets
        # the provider) instead of registering a request future that never resolves.
        await proc.stdin.drain()

    def _fail_pending(self, exc: Exception) -> None:
        pending = self._pending
        self._pending = {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
                del self._stderr_tail[:-20]


def _content_text(content: Any) -> str:
    """Extract text from a single ACP content block (``{type: text, text}``)."""
    if isinstance(content, dict) and content.get("type") == "text":
        return content.get("text") or ""
    return ""


def _tool_content_text(content: Any) -> str | None:
    """Flatten ACP tool-call content (text / diff / terminal) to a display string."""
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "content":
            inner = item.get("content")
            if isinstance(inner, dict) and inner.get("type") == "text":
                parts.append(inner.get("text") or "")
        elif kind == "text":
            parts.append(item.get("text") or "")
        elif kind == "diff":
            parts.append(f"[diff] {item.get('path') or ''}".rstrip())
        elif kind == "terminal":
            parts.append(f"[terminal {item.get('terminalId') or ''}]")
    joined = "\n".join(part for part in parts if part)
    return joined or None


def _select_option(options: list[Any], *, allow: bool) -> str | None:
    """Pick the option id matching the verdict from an ACP permission option list.

    Prefers the standard ``kind`` (allow_once / reject_once …); falls back to an
    id/name keyword match, then to a sensible default (first option for allow,
    cancel for deny). ``None`` means respond with the ``cancelled`` outcome.
    """
    want_kinds = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in want_kinds:
        for option in options:
            if isinstance(option, dict) and option.get("kind") == kind and option.get("optionId"):
                return option["optionId"]
    # Deny NEVER keyword-matches: a substring like "no" could hit an allow label
    # ("do not ask again"), so a kind-less deny falls back to cancel — always a
    # safe non-approval. Only allow has a loose fallback.
    if not allow:
        return None
    keywords = ("allow", "yes", "approve", "accept")
    for option in options:
        if not isinstance(option, dict) or not option.get("optionId"):
            continue
        text = f"{option.get('optionId', '')} {option.get('name', '')}".lower()
        if any(word in text for word in keywords):
            return option["optionId"]
    for option in options:
        if isinstance(option, dict) and option.get("optionId"):
            return option["optionId"]
    return None
