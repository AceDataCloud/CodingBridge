from types import SimpleNamespace

import pytest

from coding_bridge.config import Settings
from coding_bridge.embed import SessionHost
from coding_bridge.protocol import Action, Event


class FakeProvider:
    name = "claude"

    def __init__(self, session_id, emit, ask_permission):
        self.session_id = session_id
        self.emit = emit
        self.ask_permission = ask_permission
        self.calls = []

    async def start(self, prompt, **kwargs):
        self.calls.append(("start", prompt, kwargs))

    async def send(self, prompt, **kwargs):
        self.calls.append(("send", prompt, kwargs))

    async def edit(self, prompt, **kwargs):
        self.calls.append(("edit", prompt, kwargs))

    async def interrupt(self):
        self.calls.append(("interrupt",))

    async def aclose(self):
        self.calls.append(("close",))


@pytest.fixture
def embedded_host(tmp_path):
    events = []
    providers = []

    async def emit(payload):
        events.append(payload)

    def factory(_name, session_id, emit, ask_permission):
        provider = FakeProvider(session_id, emit, ask_permission)
        providers.append(provider)
        return provider

    settings = Settings(config_dir=tmp_path, default_cwd=str(tmp_path))
    settings.turn_retry_limit = 0
    host = SessionHost(
        settings,
        emit,
        provider_factory=factory,
        cwd_policy=lambda _cwd: str(tmp_path / "workspace"),
    )
    return SimpleNamespace(host=host, events=events, providers=providers)


@pytest.mark.asyncio
async def test_embedded_host_starts_session_with_host_cwd(embedded_host):
    await embedded_host.host.dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "local-1",
            "provider": "claude",
            "cwd": "/untrusted",
            "prompt": "inspect the logs",
            "permission_mode": "default",
        }
    )
    session = embedded_host.host.sessions["local-1"]
    await session._task

    assert session.cwd.endswith("workspace")
    assert embedded_host.providers[0].calls[0][0:2] == ("start", "inspect the logs")
    assert embedded_host.events[0]["event"] == Event.SESSION_STARTED


@pytest.mark.asyncio
async def test_embedded_host_rejects_provider_outside_allowlist(embedded_host):
    await embedded_host.host.dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "local-2",
            "provider": "codex",
            "prompt": "hello",
        }
    )

    assert embedded_host.host.sessions == {}
    assert embedded_host.events == [
        {
            "event": Event.SESSION_ERROR,
            "session_id": "local-2",
            "message": "unsupported provider: codex",
        }
    ]


@pytest.mark.asyncio
async def test_embedded_host_dispatches_follow_up_and_interrupt(embedded_host):
    await embedded_host.host.dispatch(
        {
            "action": Action.SESSION_START,
            "session_id": "local-3",
            "prompt": "first",
        }
    )
    session = embedded_host.host.sessions["local-3"]
    await session._task

    await embedded_host.host.dispatch(
        {
            "action": Action.SESSION_SEND,
            "session_id": "local-3",
            "prompt": "second",
        }
    )
    await session._task
    await embedded_host.host.dispatch(
        {"action": Action.SESSION_INTERRUPT, "session_id": "local-3"}
    )

    assert [call[0] for call in embedded_host.providers[0].calls] == [
        "start",
        "send",
        "interrupt",
    ]
