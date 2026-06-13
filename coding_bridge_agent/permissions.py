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
        # The request descriptor (tool/title/input…) kept alive for the life of
        # the future, so a browser that (re)connects after the prompt was raised
        # can re-render the dialog from a snapshot instead of missing it.
        self._details: dict[str, dict] = {}

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def pending_details(self) -> list[dict]:
        """Descriptors of every still-unresolved request, for snapshot replay."""
        return [self._details[rid] for rid in self._pending if rid in self._details]

    async def request(
        self, request_id: str, timeout: float | None = None, detail: dict | None = None
    ) -> Decision:
        """Block until the request is resolved; deny on timeout.

        ``detail`` is the request descriptor surfaced via :meth:`pending_details`
        so a late-joining browser can rebuild the approval prompt.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()
        self._pending[request_id] = future
        if detail is not None:
            self._details[request_id] = detail
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(future, timeout)
            return await future
        except (TimeoutError, asyncio.TimeoutError):
            return "deny"
        finally:
            self._pending.pop(request_id, None)
            self._details.pop(request_id, None)

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
        self._details.clear()
