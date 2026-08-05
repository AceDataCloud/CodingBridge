"""Session-picker visibility: which entrypoint the claude CLI is spawned with.

claude-agent-sdk stamps ``CLAUDE_CODE_ENTRYPOINT=sdk-py`` on every transcript it
writes, and both the VSCode extension and ``claude --resume`` filter those out of
their pickers as "programmatic". Overriding it via ``options.env`` — which the SDK
merges *after* its own default — is what puts bridge sessions back in the list.
"""

import sys
import types

import pytest

from coding_bridge.config import DEFAULT_CLAUDE_ENTRYPOINT, Settings
from coding_bridge.providers.claude import ClaudeProvider


class _FakeOptions:
    """Stands in for ClaudeAgentOptions: a plain attribute bag with env={}."""

    def __init__(self, **kwargs):
        self.env: dict[str, str] = {}
        self.extra_args: dict | None = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeClient:
    last_options: _FakeOptions | None = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stub ``claude_agent_sdk`` so no real CLI is spawned."""
    module = types.ModuleType("claude_agent_sdk")
    module.ClaudeAgentOptions = _FakeOptions
    module.ClaudeSDKClient = _FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    _FakeClient.last_options = None
    return module


async def _connect(settings, fake_sdk, **kwargs):
    async def emit(_payload):
        return None

    async def ask(*_args):
        return "deny"

    provider = ClaudeProvider("s1", emit, ask, settings)
    # _load_server_info would query the stub client; the options are all we assert on.
    provider._load_server_info = lambda: _noop()  # type: ignore[method-assign]
    await provider._ensure_client(
        cwd="/tmp", model=None, permission_mode="default", **kwargs
    )
    return _FakeClient.last_options


async def _noop():
    return None


async def test_default_entrypoint_is_visible_in_pickers(fake_sdk):
    """Out of the box a bridge session must not be stamped as programmatic."""
    options = await _connect(Settings(), fake_sdk)
    assert options.env["CLAUDE_CODE_ENTRYPOINT"] == DEFAULT_CLAUDE_ENTRYPOINT
    assert options.env["CLAUDE_CODE_ENTRYPOINT"] not in {"sdk-py", "sdk-ts", "sdk-cli"}


async def test_resume_uses_compatibility_repair(fake_sdk, monkeypatch):
    monkeypatch.setattr(
        "coding_bridge.providers.claude.claude_transcript.prepare_resume",
        lambda session_id: "77777777-7777-7777-7777-777777777777",
    )

    options = await _connect(Settings(), fake_sdk, resume="legacy-session")

    assert options.resume == "77777777-7777-7777-7777-777777777777"


async def test_fresh_session_skips_compatibility_repair(fake_sdk, monkeypatch):
    def fail(_session_id):
        raise AssertionError("fresh sessions must not inspect transcripts")

    monkeypatch.setattr(
        "coding_bridge.providers.claude.claude_transcript.prepare_resume", fail
    )

    options = await _connect(Settings(), fake_sdk)

    assert options.resume is None


async def test_entrypoint_is_overridable(fake_sdk):
    """Operators can opt back into the hidden, SDK-native entrypoint."""
    options = await _connect(Settings(claude_entrypoint="sdk-py"), fake_sdk)
    assert options.env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"


@pytest.mark.parametrize("value", ["", "   "])
async def test_blank_entrypoint_leaves_sdk_default(fake_sdk, value):
    """A blank override must not stamp an empty entrypoint the CLI would reject."""
    options = await _connect(Settings(claude_entrypoint=value), fake_sdk)
    assert "CLAUDE_CODE_ENTRYPOINT" not in options.env


def test_from_env_reads_override(monkeypatch):
    monkeypatch.setenv("CODING_BRIDGE_CLAUDE_ENTRYPOINT", "sdk-py")
    assert Settings.from_env().claude_entrypoint == "sdk-py"


def test_from_env_defaults_to_visible(monkeypatch):
    monkeypatch.delenv("CODING_BRIDGE_CLAUDE_ENTRYPOINT", raising=False)
    assert Settings.from_env().claude_entrypoint == DEFAULT_CLAUDE_ENTRYPOINT
