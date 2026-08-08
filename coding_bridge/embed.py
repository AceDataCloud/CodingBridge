"""Embedding surface for hosting Coding Bridge sessions without the relay transport."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .config import Settings
from .connection import BridgeConnection
from .protocol import Action, Event, event_payload
from .providers.base import EmitFn, ProviderFactory

CwdPolicy = Callable[[str | None], str]


class SessionHost(BridgeConnection):
    """Run Coding Bridge sessions behind a caller-owned transport."""

    def __init__(
        self,
        settings: Settings,
        emit: EmitFn,
        *,
        provider_factory: ProviderFactory | None = None,
        providers: Iterable[str] = ("claude",),
        cwd_policy: CwdPolicy | None = None,
    ) -> None:
        super().__init__(settings, "embedded", provider_factory=provider_factory)
        self._host_emit = emit
        self._providers = frozenset(providers)
        self._cwd_policy = cwd_policy or (lambda cwd: cwd or settings.default_cwd)

    async def send_payload(self, payload: dict[str, Any]) -> None:
        """Deliver one inner event to the embedding application's transport."""
        await self._host_emit(payload)

    async def dispatch(self, payload: dict[str, Any]) -> None:
        """Dispatch one browser action after applying host-owned policy."""
        action = payload.get("action")
        prepared = dict(payload)
        if action == Action.SESSION_START:
            provider = prepared.get("provider") or "claude"
            if provider not in self._providers:
                await self.send_payload(
                    event_payload(
                        Event.SESSION_ERROR,
                        prepared.get("session_id"),
                        message=f"unsupported provider: {provider}",
                    )
                )
                return
            prepared["provider"] = provider
            prepared["cwd"] = self._cwd_policy(prepared.get("cwd"))
        await self._dispatch(prepared)


__all__ = ["CwdPolicy", "SessionHost"]
