"""Extra tests for the P7 additions to ``WeChatClient`` — task-status polling."""

from __future__ import annotations

import httpx
import pytest

from coding_bridge.channels.wechat import WeChatClient


@pytest.mark.asyncio
async def test_get_task_status_returns_json_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tasks/task-42"
        assert request.headers.get("authorization") == "Bearer tok"
        return httpx.Response(200, json={"status": "delivered", "task_id": "task-42"})

    transport = httpx.MockTransport(handler)
    client = WeChatClient("http://wechat.local", "tok", transport=transport)
    try:
        body = await client.get_task_status("task-42")
    finally:
        await client.aclose()
    assert body == {"status": "delivered", "task_id": "task-42"}


@pytest.mark.asyncio
async def test_get_task_status_rejects_empty_id() -> None:
    client = WeChatClient(
        "http://wechat.local", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    try:
        with pytest.raises(ValueError, match="task_id must not be empty"):
            await client.get_task_status("")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_task_status_rejects_query_injection() -> None:
    # Attacker tries to inject a query parameter or a second path segment
    # via `task_id` — the client refuses BEFORE any network call.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = WeChatClient("http://wechat.local", "tok", transport=httpx.MockTransport(handler))
    try:
        for bad in [
            "42?admin=1",
            "42#frag",
            "../admin",
            "42/../../root",
            "task with spaces",
            "abc%2Fdef",  # already-encoded slash
            "\n42",
        ]:
            with pytest.raises(ValueError, match="invalid characters"):
                await client.get_task_status(bad)
        # None of the rejected task ids reached the transport
        assert seen == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_task_status_accepts_safe_uuid_shaped_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Ensure the path was NOT tampered with
        assert request.url.path == "/api/tasks/abc-DEF_123"
        assert request.url.query == b""
        return httpx.Response(200, json={"status": "ok"})

    client = WeChatClient("http://wechat.local", "tok", transport=httpx.MockTransport(handler))
    try:
        assert await client.get_task_status("abc-DEF_123") == {"status": "ok"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_task_status_raises_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "unknown task"})

    transport = httpx.MockTransport(handler)
    client = WeChatClient("http://wechat.local", "tok", transport=transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_task_status("missing")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_task_status_tolerates_non_dict_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    transport = httpx.MockTransport(handler)
    client = WeChatClient("http://wechat.local", "tok", transport=transport)
    try:
        assert await client.get_task_status("x") == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_task_status_tolerates_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"plain text", headers={"Content-Type": "text/plain"})

    transport = httpx.MockTransport(handler)
    client = WeChatClient("http://wechat.local", "tok", transport=transport)
    try:
        assert await client.get_task_status("x") == {}
    finally:
        await client.aclose()
