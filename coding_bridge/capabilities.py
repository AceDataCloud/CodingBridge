"""Advertise what this node can do so the browser never hard-codes options.

The browser asks (`capabilities.get`) and the node answers (`capabilities`)
with the providers it supports, the models/effort tiers/permission modes each
one offers, and whether the backing CLI is installed. The catalogs live HERE,
on the node, so a new model or effort tier only needs a node update (or a custom
value typed in the box) — the web UI renders whatever the node reports.

Each provider also advertises its slash `commands`. For Claude these come from
the SDK's per-environment `get_server_info()` — the authoritative list of what
actually runs headlessly (built-ins like `/context`, `/compact`, plus the user's
own `.claude/commands`). The browser uses it for `/` autocomplete so the user
discovers exactly what their machine supports.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("coding-bridge.capabilities")

# Per-provider model catalogs. Labels are proper nouns shown verbatim by the
# browser; `value` is what we pass to the CLI. Update this list (or ship a new
# node release) when a backend adds a model and every browser picks it up.
_CLAUDE_MODELS: list[dict[str, str]] = [
    {"value": "sonnet", "label": "Claude Sonnet"},
    {"value": "opus", "label": "Claude Opus"},
    {"value": "haiku", "label": "Claude Haiku"},
]
_CODEX_MODELS: list[dict[str, str]] = [
    {"value": "gpt-5-codex", "label": "GPT-5 Codex"},
    {"value": "gpt-5", "label": "GPT-5"},
    {"value": "o3", "label": "o3"},
]

# Effort tokens are semantic; the browser localizes known ones and shows the raw
# token for anything new. "" means "use the backend default".
_CLAUDE_EFFORTS: list[str] = ["", "low", "medium", "high", "max"]
_CODEX_EFFORTS: list[str] = ["", "low", "medium", "high"]

# Permission modes are shared; they map to provider sandboxes in each provider.
_PERMISSION_MODES: list[str] = ["default", "acceptEdits", "plan", "bypassPermissions"]


def _candidate_cli_paths(cli: str) -> list[str]:
    """Well-known install locations a daemon's PATH commonly omits.

    A node launched outside the user's login shell (no nvm/volta/asdf shims, no
    ``~/.local/bin``) has a bare PATH, so ``shutil.which`` misses a CLI that is
    in fact installed. Probing these dirs makes ``available`` reflect reality.
    """
    home = Path.home()
    out: list[str] = []
    # nvm keeps each node version's globals in its own bin dir, none of which is
    # on PATH unless nvm was sourced — the #1 reason `claude` looks "missing".
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        out += [str(p) for p in sorted(nvm_root.glob(f"*/bin/{cli}"), reverse=True)]
    fixed = [
        home / ".local" / "bin" / cli,
        home / ".npm-global" / "bin" / cli,
        home / ".volta" / "bin" / cli,
        Path("/opt/homebrew/bin") / cli,
        Path("/usr/local/bin") / cli,
    ]
    if cli == "claude":
        fixed.insert(0, home / ".claude" / "local" / "claude")  # native installer
    out += [str(p) for p in fixed]
    return out


def resolve_cli(cli: str, settings: Any | None = None) -> str | None:
    """Absolute path to a provider CLI, or None if genuinely absent.

    Resolution order: explicit ``settings.<cli>_path`` override → PATH
    (``shutil.which``) → well-known install dirs a daemon's PATH commonly misses.
    The last step is why a node started without the user's shell PATH still
    detects ``claude``/``codex`` instead of falsely reporting them uninstalled.
    """
    override = getattr(settings, f"{cli}_path", None) if settings is not None else None
    if override:
        expanded = Path(override).expanduser()
        if expanded.is_file():
            return str(expanded)
    found = shutil.which(cli)
    if found:
        return found
    for cand in _candidate_cli_paths(cli):
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def ensure_clis_on_path(settings: Any | None = None) -> list[str]:
    """Prepend each resolved CLI's directory to PATH so it can actually launch.

    ``resolve_cli`` can find a binary by absolute path, but claude-agent-sdk and
    ``codex exec`` still look it up on PATH — so a daemon with a bare PATH would
    detect the CLI yet fail to start it. Called once at startup. Returns the dirs
    added, for logging.
    """
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    added: list[str] = []
    for cli in ("claude", "codex"):
        resolved = resolve_cli(cli, settings)
        if not resolved:
            continue
        parent = str(Path(resolved).parent)
        if parent and parent not in path_entries and parent not in added:
            added.append(parent)
    if added:
        os.environ["PATH"] = os.pathsep.join([*added, os.environ.get("PATH", "")])
    return added


def _provider(
    name: str,
    label: str,
    cli: str,
    models: list[dict[str, str]],
    efforts: list[str],
    *,
    settings: Any | None = None,
    supports_edit: bool = False,
    supports_code_restore: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "available": resolve_cli(cli, settings) is not None,
        "models": models,
        "efforts": efforts,
        "permission_modes": list(_PERMISSION_MODES),
        "allow_custom_model": True,
        # Whether a past prompt can be edited (the conversation forked at that
        # turn). Claude has first-class `--resume-session-at` / `--fork-session`;
        # Codex has no such primitive, so its prompts are not editable.
        "supports_edit": supports_edit,
        # Whether editing can also roll back on-disk file changes (file
        # checkpointing), so the browser can offer a "restore code" choice.
        "supports_code_restore": supports_code_restore,
    }


def describe(settings: Any | None = None) -> dict[str, Any]:
    """Build the capabilities descriptor sent to the browser."""
    return {
        "providers": [
            _provider(
                "claude",
                "Claude Code",
                "claude",
                _CLAUDE_MODELS,
                _CLAUDE_EFFORTS,
                settings=settings,
                supports_edit=True,
                supports_code_restore=True,
            ),
            _provider(
                "codex", "Codex", "codex", _CODEX_MODELS, _CODEX_EFFORTS, settings=settings
            ),
        ],
    }


async def describe_detailed(settings: Any) -> dict[str, Any]:
    """`describe()` enriched with each provider's slash-command catalog.

    Claude's catalog is fetched once from the SDK (cached); Codex's is derived
    from its local custom-prompt directory. Falls back to an empty catalog if a
    backend is unavailable or probing fails — the UI just shows no autocomplete.
    """
    desc = describe(settings)
    claude_commands = await _claude_commands(settings)
    codex_commands = _codex_commands()
    for provider in desc["providers"]:
        if provider["name"] == "claude":
            provider["commands"] = claude_commands
        elif provider["name"] == "codex":
            provider["commands"] = codex_commands
    return desc


def normalize_commands(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Map a `get_server_info()` payload to the wire shape the browser expects."""
    raw = (info or {}).get("commands") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or not isinstance(name, str):
            continue
        aliases = [a for a in (entry.get("aliases") or []) if isinstance(a, str)]
        out.append(
            {
                "name": name,
                "description": entry.get("description") or "",
                "argument_hint": entry.get("argumentHint") or entry.get("argument_hint") or "",
                "aliases": aliases,
            }
        )
    return out


def command_name_set(commands: list[dict[str, Any]] | None) -> set[str]:
    """Lower-cased set of every command name and alias, for fast membership checks."""
    names: set[str] = set()
    for cmd in commands or []:
        name = cmd.get("name")
        if isinstance(name, str):
            names.add(name.lower())
        for alias in cmd.get("aliases") or []:
            if isinstance(alias, str):
                names.add(alias.lower())
    return names


_claude_commands_cache: list[dict[str, Any]] | None = None
_claude_commands_lock = asyncio.Lock()


async def _claude_commands(settings: Any) -> list[dict[str, Any]]:
    """Fetch Claude's per-environment slash-command catalog once and cache it.

    Spins up a throwaway streaming SDK client purely to read the `initialize`
    response (`get_server_info()`), which lists every command the CLI accepts in
    this environment. Cheap enough to do once on the first `capabilities.get`.
    """
    global _claude_commands_cache
    if _claude_commands_cache is not None:
        return _claude_commands_cache
    async with _claude_commands_lock:
        if _claude_commands_cache is not None:
            return _claude_commands_cache
        commands = await _probe_claude_commands(settings)
        _claude_commands_cache = commands
        return commands


async def _probe_claude_commands(settings: Any) -> list[dict[str, Any]]:
    if resolve_cli("claude", settings) is None:
        return []
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    except ImportError:
        return []
    try:
        options = ClaudeAgentOptions(
            cwd=(getattr(settings, "default_cwd", "") or None),
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=45)
        try:
            info = await asyncio.wait_for(client.get_server_info(), timeout=15)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
        return normalize_commands(info)
    except Exception as exc:  # noqa: BLE001 - never let probing break capabilities
        logger.warning("could not probe claude commands: %s", exc)
        return []


def _codex_commands() -> list[dict[str, Any]]:
    """Codex custom prompts (`$CODEX_HOME/prompts/*.md`) surfaced as slash commands.

    `codex exec` is non-interactive and has no built-in slash processor, so only
    user-defined prompt files are advertised; the rest of Codex's interactive
    slash commands cannot run remotely.
    """
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    prompts_dir = Path(home) / "prompts"
    if not prompts_dir.is_dir():
        return []
    commands: list[dict[str, Any]] = []
    try:
        entries = sorted(prompts_dir.glob("*.md"))
    except OSError:
        return []
    for path in entries:
        name = path.stem
        if not name:
            continue
        commands.append({"name": name, "description": "", "argument_hint": "", "aliases": []})
    return commands
