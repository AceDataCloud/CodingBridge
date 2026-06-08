"""Pending tool-approval registry.

Each in-flight ``can_use_tool`` call parks on a future keyed by request id. The
browser's ``permission.resolve`` (or a timeout) settles it. This is the seam
that turns a local agent prompt into a remote approval.
"""
from __future__ import annotations

import asyncio
from typing import Literal

Decision = Literal["allow", "deny"]


class PermissionBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    async def request(self, request_id: str, timeout: float | None = None) -> Decision:
        """Block until the request is resolved; deny on timeout."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()
        self._pending[request_id] = future
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(future, timeout)
            return await future
        except (TimeoutError, asyncio.TimeoutError):
            return "deny"
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, decision: Decision) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result("allow" if decision == "allow" else "deny")
        return True

    def cancel_all(self, decision: Decision = "deny") -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(decision)
        self._pending.clear()
