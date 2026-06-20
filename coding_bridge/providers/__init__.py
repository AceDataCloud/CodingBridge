"""Provider registry."""
from __future__ import annotations

from ..config import Settings
from .base import AskPermissionFn, EmitFn, Provider, ProviderFactory
from .claude import ClaudeProvider
from .codex import CodexProvider

KNOWN_PROVIDERS = ("claude", "codex")

__all__ = [
    "KNOWN_PROVIDERS",
    "ClaudeProvider",
    "CodexProvider",
    "Provider",
    "ProviderFactory",
    "default_provider_factory",
]


def default_provider_factory(settings: Settings) -> ProviderFactory:
    def factory(
        provider: str, session_id: str, emit: EmitFn, ask_permission: AskPermissionFn
    ) -> Provider:
        if provider == "codex":
            return CodexProvider(session_id, emit, ask_permission, settings)
        return ClaudeProvider(session_id, emit, ask_permission, settings)

    return factory
