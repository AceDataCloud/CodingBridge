"""Advertise what this node can do so the browser never hard-codes options.

The browser asks (`capabilities.get`) and the node answers (`capabilities`)
with the providers it supports, the models/effort tiers/permission modes each
one offers, and whether the backing CLI is installed. The catalogs live HERE,
on the node, so a new model or effort tier only needs a node update (or a custom
value typed in the box) — the web UI renders whatever the node reports.
"""
from __future__ import annotations

import shutil
from typing import Any

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


def _provider(
    name: str,
    label: str,
    cli: str,
    models: list[dict[str, str]],
    efforts: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "available": shutil.which(cli) is not None,
        "models": models,
        "efforts": efforts,
        "permission_modes": list(_PERMISSION_MODES),
        "allow_custom_model": True,
    }


def describe() -> dict[str, Any]:
    """Build the capabilities descriptor sent to the browser."""
    return {
        "providers": [
            _provider("claude", "Claude Code", "claude", _CLAUDE_MODELS, _CLAUDE_EFFORTS),
            _provider("codex", "Codex", "codex", _CODEX_MODELS, _CODEX_EFFORTS),
        ],
    }
