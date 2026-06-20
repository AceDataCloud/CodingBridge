"""Codex provider backed by the ``codex exec --json`` CLI.

Runs OpenAI Codex non-interactively and relays its JSONL event stream as the same
inner events the Claude provider emits. ``codex exec`` is non-interactive, so the
session ``permission_mode`` maps to a sandbox policy rather than per-tool prompts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import uuid
from typing import TYPE_CHECKING, Any

from .. import attachments as attachment_store
from .. import images as image_store
from ..protocol import Event, event_payload
from .base import slash_name

if TYPE_CHECKING:
    from ..config import Settings
    from .base import AskPermissionFn, EmitFn

logger = logging.getLogger("coding-bridge-agent.codex")

# permission_mode -> codex sandbox policy (exec has no interactive approvals).
_SANDBOX_BY_MODE = {
    "plan": "read-only",
    "default": "workspace-write",
    "acceptEdits": "workspace-write",
    "bypassPermissions": "danger-full-access",
}
_DEFAULT_SANDBOX = "workspace-write"
_EFFORT_ALIASES = {"max": "high", "ultra-high": "high", "ultrahigh": "high"}
_EFFORT_VALUES = {"minimal", "low", "medium", "high"}

# Typewriter cadence for codex agent messages. ``codex exec`` delivers the final
# message whole, so we replay it as text deltas for visual streaming parity with
# Claude. Tests patch STREAM_DELAY to 0 for determinism.
STREAM_CHUNK_TARGET = 80
STREAM_MIN_CHUNK = 3
STREAM_DELAY = 0.012


def _codex_effort(effort: str | None) -> str | None:
    if not effort:
        return None
    value = _EFFORT_ALIASES.get(effort, effort)
    return value if value in _EFFORT_VALUES else None


class CodexProvider:
    name = "codex"

    def __init__(
        self,
        session_id: str,
        emit: EmitFn,
        ask_permission: AskPermissionFn,
        settings: Settings,
    ) -> None:
        self._session_id = session_id
        self._emit = emit
        self._ask = ask_permission  # exec is non-interactive; kept for protocol parity
        self._settings = settings
        self._cwd = settings.default_cwd
        self._model: str | None = settings.default_model
        self._sandbox = _DEFAULT_SANDBOX
        self._effort: str | None = None
        self._thread_id: str | None = None
        self._last_error: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        # Announced the real (thread) id to the browser yet? See claude.py.
        self._announced_identity = False

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
        self._sandbox = _SANDBOX_BY_MODE.get(permission_mode, _DEFAULT_SANDBOX)
        self._effort = _codex_effort(effort)
        self._thread_id = resume or None
        if await self._maybe_handle_slash(prompt):
            return
        await self._run_turn(prompt, resume=bool(resume), images=images, attachments=attachments)

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
        if await self._maybe_handle_slash(prompt):
            return
        # exec spawns a fresh process per turn, so a new model/effort/sandbox
        # simply applies to the next resumed turn.
        self._model = model
        self._effort = _codex_effort(effort)
        if permission_mode:
            self._sandbox = _SANDBOX_BY_MODE.get(permission_mode, _DEFAULT_SANDBOX)
        await self._run_turn(
            prompt, resume=self._thread_id is not None, images=images, attachments=attachments
        )

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
        """Editing is unsupported for Codex.

        ``codex exec resume`` only continues a thread — there is no truncate or
        fork primitive — so a past prompt can't be rewritten without rewriting
        Codex's internal rollout file. Capabilities advertise this off, so this
        is only a defensive guard; report it cleanly instead of forking blindly.
        """
        await self._emit(
            event_payload(
                Event.SESSION_NOTICE,
                self._session_id,
                level="info",
                code="edit_unsupported",
                text="Editing a past prompt isn't supported for Codex sessions.",
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

    async def _maybe_handle_slash(self, prompt: str) -> bool:
        """Codex `exec` has no slash processor, so report commands as unavailable.

        Returns True when a slash command was intercepted with a localized
        notice so the caller skips spawning ``codex exec``.
        """
        name = slash_name(prompt)
        if name is None:
            return False
        await self._emit(
            event_payload(
                Event.SESSION_NOTICE,
                self._session_id,
                level="info",
                code="slash_codex_unsupported",
                command=name,
                text=(
                    f"/{name} is a Codex interactive command and isn't available "
                    "in a remote Coding Bridge session."
                ),
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
        return True

    def _build_argv(self, prompt: str, *, resume: bool, image_paths: list[str]) -> list[str]:
        thread_id = self._thread_id if resume else None
        argv = ["codex", "exec"]
        if thread_id:
            argv.append("resume")
        argv += ["--json", "--skip-git-repo-check"]
        # `codex exec` takes -s/--sandbox, but `codex exec resume` rejects it (its
        # only sandbox knob is a -c config override) — passing -s there aborts the
        # turn with `unexpected argument '-s'`, so set the policy via -c on resume.
        if thread_id:
            argv += ["-c", f'sandbox_mode="{self._sandbox}"']
        else:
            argv += ["-s", self._sandbox]
        if self._model:
            argv += ["-m", self._model]
        if self._effort:
            argv += ["-c", f"model_reasoning_effort={self._effort}"]
        # Each image is its own -i (resume's -i is single-valued; never collapse).
        for path in image_paths:
            argv += ["-i", path]
        # `--` terminates option parsing so a prompt starting with `-`, and
        # `exec`'s variadic -i, can't swallow the prompt / misparse positionals.
        # resume positionals are [SESSION_ID] [PROMPT]; a fresh exec takes [PROMPT].
        argv.append("--")
        if thread_id:
            argv += [thread_id, prompt]
        else:
            argv.append(prompt)
        return argv

    async def _run_turn(
        self,
        prompt: str,
        *,
        resume: bool,
        images: list | None = None,
        attachments: list | None = None,
    ) -> None:
        if shutil.which("codex") is None:
            raise RuntimeError(
                "codex CLI is not installed; install it from https://github.com/openai/codex"
            )
        legacy_image_paths = image_store.save_images(images, self._cwd, session_id=self._session_id)
        files = attachment_store.save_attachments(
            attachments, self._cwd, session_id=self._session_id
        )
        image_paths = legacy_image_paths + attachment_store.image_paths(files)
        prompt = attachment_store.attachment_note(prompt, files, legacy_image_paths)
        argv = self._build_argv(prompt, resume=resume, image_paths=image_paths)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._cwd or None,
            stdin=asyncio.subprocess.DEVNULL,  # never block reading an inherited stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._proc
        stderr_tail: list[str] = []
        drain = asyncio.create_task(self._drain_stderr(proc, stderr_tail))
        self._last_error = None
        saw_result = False
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if await self._handle_event(obj):
                    saw_result = True
        finally:
            await proc.wait()
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
            self._proc = None
        if proc.returncode not in (0, None) and not saw_result:
            detail = (
                self._last_error
                or " ".join(stderr_tail[-5:]).strip()
                or f"codex exited with {proc.returncode}"
            )
            await self._emit(event_payload(Event.SESSION_ERROR, self._session_id, message=detail))

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, tail: list[str]) -> None:
        if proc.stderr is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                tail.append(raw.decode("utf-8", "replace").rstrip())
                del tail[:-20]

    async def _maybe_announce_identity(self) -> None:
        """Adopt the codex thread id as this session's canonical id, once known.

        Mirrors the Claude provider: the browser opens under a provisional id,
        codex assigns the real thread id at ``thread.started``; we emit
        session.identified and re-tag later events so live + history share one id.
        """
        if self._announced_identity:
            return
        sid = self._thread_id
        if not sid:
            return
        self._announced_identity = True
        if sid == self._session_id:
            return
        old = self._session_id
        await self._emit(event_payload(Event.SESSION_IDENTIFIED, old, sdk_session_id=sid))
        self._session_id = sid

    async def _handle_event(self, obj: dict[str, Any]) -> bool:
        """Map one codex JSONL event; returns True when it ends a turn."""
        kind = obj.get("type")
        if kind == "thread.started":
            self._thread_id = obj.get("thread_id") or self._thread_id
            await self._maybe_announce_identity()
        elif kind in ("item.started", "item.updated", "item.completed"):
            await self._handle_item(kind, obj.get("item") or {})
        elif kind == "turn.completed":
            await self._emit(
                event_payload(
                    Event.SESSION_RESULT,
                    self._session_id,
                    subtype="turn_complete",
                    is_error=False,
                    usage=obj.get("usage"),
                    sdk_session_id=self._thread_id,
                )
            )
            return True
        elif kind == "turn.failed":
            message = _error_message(obj) or "codex turn failed"
            await self._emit(event_payload(Event.SESSION_ERROR, self._session_id, message=message))
            return True
        elif kind == "error":
            # Transient stream notice (e.g. "Reconnecting... 2/5"); the turn often
            # recovers. Remember the last one and only surface it if the process
            # exits without completing a turn.
            self._last_error = _error_message(obj) or self._last_error
        return False

    async def _handle_item(self, phase: str, item: dict[str, Any]) -> None:
        itype = item.get("type")
        completed = phase == "item.completed"
        if itype == "agent_message":
            text = item.get("text")
            if completed and text:
                await self._emit_text_stream(text)
        elif itype == "reasoning":
            text = item.get("text")
            if completed and text:
                await self._emit(event_payload(Event.SESSION_THINKING, self._session_id, text=text))
        elif itype == "command_execution":
            if phase == "item.started":
                await self._emit(
                    event_payload(
                        Event.SESSION_TOOL_USE,
                        self._session_id,
                        tool="shell",
                        tool_use_id=item.get("id"),
                        input={"command": item.get("command")},
                    )
                )
            elif completed:
                await self._emit(
                    event_payload(
                        Event.SESSION_TOOL_RESULT,
                        self._session_id,
                        tool_use_id=item.get("id"),
                        content=item.get("aggregated_output") or item.get("output"),
                        is_error=item.get("exit_code") not in (0, None),
                    )
                )
        elif itype in ("file_change", "patch"):
            if completed:
                await self._emit(
                    event_payload(
                        Event.SESSION_TOOL_USE,
                        self._session_id,
                        tool="edit",
                        tool_use_id=item.get("id"),
                        input={"changes": item.get("changes")},
                    )
                )
        elif itype == "mcp_tool_call":
            if completed:
                await self._emit(
                    event_payload(
                        Event.SESSION_TOOL_USE,
                        self._session_id,
                        tool=item.get("tool") or "mcp",
                        tool_use_id=item.get("id"),
                        input=item.get("arguments"),
                    )
                )
        elif completed and item.get("text"):
            await self._emit_text_stream(item["text"])

    async def _emit_text_stream(self, text: str) -> None:
        """Replay a whole agent message as incremental text deltas + a commit.

        ``codex exec`` has no native token streaming, so we chunk the final
        message into deltas (capped chunk count) to mirror Claude's streaming
        UX, then emit an authoritative ``session.text`` carrying the same id.
        """
        stream_id = f"{self._session_id}:{uuid.uuid4().hex[:8]}"
        size = max(STREAM_MIN_CHUNK, math.ceil(len(text) / STREAM_CHUNK_TARGET))
        for start in range(0, len(text), size):
            chunk = text[start : start + size]
            await self._emit(
                event_payload(
                    Event.SESSION_TEXT_DELTA, self._session_id, text=chunk, id=stream_id
                )
            )
            if STREAM_DELAY:
                await asyncio.sleep(STREAM_DELAY)
        await self._emit(
            event_payload(Event.SESSION_TEXT, self._session_id, text=text, id=stream_id)
        )

    async def interrupt(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()

    async def aclose(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        self._proc = None


def _error_message(obj: dict[str, Any]) -> str | None:
    error = obj.get("error")
    if isinstance(error, dict):
        return error.get("message")
    if isinstance(error, str):
        return error
    return obj.get("message")
