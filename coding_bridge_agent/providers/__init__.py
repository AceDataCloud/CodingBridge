"""Provider registry."""
from __future__ import annotations

from ..config import Settings
from .base import AskPermissionFn, EmitFn, Provider, ProviderFactory
from .claude import ClaudeProvider

__all__ = ["ClaudeProvider", "Provider", "ProviderFactory", "default_provider_factory"]


def default_provider_factory(settings: Settings) -> ProviderFactory:
    def factory(session_id: str, emit: EmitFn, ask_permission: AskPermissionFn) -> Provider:
        return ClaudeProvider(session_id, emit, ask_permission, settings)

    return factory
