import asyncio

from coding_bridge.permissions import PermissionBroker, Resolution


async def test_resolve_allow():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    assert broker.resolve("r1", "allow") is True
    assert await task == Resolution("allow")


async def test_resolve_deny_normalizes():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    assert broker.resolve("r1", "whatever") is True
    assert await task == Resolution("deny")


async def test_resolve_allow_carries_answer():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    answer = {"answers": {"Pick one": "A"}}
    assert broker.resolve("r1", "allow", answer) is True
    assert await task == Resolution("allow", answer)


async def test_deny_never_carries_answer():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    # An answer attached to a deny is discarded — a deny is always answer-less.
    assert broker.resolve("r1", "deny", {"answers": {"q": "a"}}) is True
    assert await task == Resolution("deny", None)


async def test_timeout_denies():
    broker = PermissionBroker()
    assert await broker.request("r2", timeout=0.01) == Resolution("deny")


async def test_cancel_all():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r3", timeout=5))
    await asyncio.sleep(0)
    broker.cancel_all("deny")
    assert await task == Resolution("deny")


async def test_resolve_unknown_returns_false():
    broker = PermissionBroker()
    assert broker.resolve("missing", "allow") is False


async def test_pending_details_tracks_inflight_requests():
    broker = PermissionBroker()
    detail = {"request_id": "r1", "tool": "Read", "session_id": "s1"}
    task = asyncio.create_task(broker.request("r1", timeout=5, detail=detail))
    await asyncio.sleep(0)
    assert broker.pending_details() == [detail]
    assert broker.resolve("r1", "allow") is True
    assert await task == Resolution("allow")
    # Detail is dropped once the request settles.
    assert broker.pending_details() == []


async def test_pending_details_cleared_on_timeout():
    broker = PermissionBroker()
    assert await broker.request("r2", timeout=0.01, detail={"request_id": "r2"}) == Resolution(
        "deny"
    )
    assert broker.pending_details() == []
