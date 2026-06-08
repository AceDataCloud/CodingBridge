import asyncio

from coding_bridge_agent.permissions import PermissionBroker


async def test_resolve_allow():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    assert broker.resolve("r1", "allow") is True
    assert await task == "allow"


async def test_resolve_deny_normalizes():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r1", timeout=5))
    await asyncio.sleep(0)
    assert broker.resolve("r1", "whatever") is True
    assert await task == "deny"


async def test_timeout_denies():
    broker = PermissionBroker()
    assert await broker.request("r2", timeout=0.01) == "deny"


async def test_cancel_all():
    broker = PermissionBroker()
    task = asyncio.create_task(broker.request("r3", timeout=5))
    await asyncio.sleep(0)
    broker.cancel_all("deny")
    assert await task == "deny"


async def test_resolve_unknown_returns_false():
    broker = PermissionBroker()
    assert broker.resolve("missing", "allow") is False
