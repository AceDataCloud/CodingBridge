"""Editing a past prompt: fork the transcript and re-run from that point.

Claude rewinds the conversation with ``--fork-session --resume-session-at`` so
the edited turn replaces the old one (the original session stays intact). These
tests cover the fork wiring, the fork point reported on each result, optional
code restore, the capability flags, and that Codex reports edit unsupported.
"""

import json

from coding_bridge_agent import capabilities, history
from coding_bridge_agent.config import Settings
from coding_bridge_agent.providers.claude import ClaudeProvider
from coding_bridge_agent.providers.codex import CodexProvider


def _capturing(provider_cls=ClaudeProvider):
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(*_args):
        return "deny"

    return provider_cls("s1", emit, ask, Settings()), events


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _Assistant:
    def __init__(self, content: list, uuid: str | None, session_id: str | None = None):
        self.content = content
        self.uuid = uuid
        self.session_id = session_id


class _Result:
    subtype = "success"
    is_error = False
    result = None
    total_cost_usd = 0.0

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id


# --- fork point reported on the result --------------------------------------
async def test_result_reports_fork_point():
    """Each turn's result carries the last message uuid + sdk session id."""
    provider, events = _capturing()
    await provider._handle_message(_Assistant([_TextBlock("hi")], uuid="U_LAST", session_id="SDK1"))
    await provider._handle_message(_Result(session_id="SDK1"))
    result = [e for e in events if e["event"] == "session.result"][0]
    assert result["cut_uuid"] == "U_LAST"
    assert result["sdk_session_id"] == "SDK1"


# --- edit() forks at the cut point ------------------------------------------
def _stub_run(provider, monkeypatch):
    calls: dict = {}

    async def fake_ensure(**kwargs):
        calls.update(kwargs)
        provider._connected = True

    async def fake_turn(prompt):
        calls["prompt"] = prompt

    monkeypatch.setattr(provider, "_ensure_client", fake_ensure)
    monkeypatch.setattr(provider, "_turn", fake_turn)
    return calls


async def test_edit_forks_with_resume_session_at(monkeypatch):
    provider, _ = _capturing()
    provider._sdk_session_id = "SDK_ABC"
    provider._connected = True

    closed = {"n": 0}

    class _Client:
        async def disconnect(self):
            closed["n"] += 1

    provider._client = _Client()
    calls = _stub_run(provider, monkeypatch)

    await provider.edit("edited prompt", cut_uuid="U_KEEP", model="opus")

    assert calls["resume"] == "SDK_ABC"
    assert calls["fork_session"] is True
    assert calls["extra_args"] == {"resume-session-at": "U_KEEP"}
    assert calls["model"] == "opus"
    assert calls["prompt"] == "edited prompt"
    assert closed["n"] == 1  # the original client was torn down before forking


async def test_edit_first_prompt_starts_fresh(monkeypatch):
    """Editing the first prompt (no cut point) starts a brand-new session."""
    provider, _ = _capturing()
    provider._sdk_session_id = "SDK_ABC"
    provider._connected = True

    class _Client:
        async def disconnect(self):
            pass

    provider._client = _Client()
    calls = _stub_run(provider, monkeypatch)

    await provider.edit("rewritten first", cut_uuid=None)

    assert calls["resume"] is None
    assert calls["fork_session"] is False
    assert calls["extra_args"] is None


async def test_edit_restore_code_rewinds_files(monkeypatch):
    provider, _ = _capturing()
    provider._sdk_session_id = "SDK_ABC"
    provider._connected = True

    rewound: dict = {}

    class _Client:
        async def rewind_files(self, user_message_id):
            rewound["uid"] = user_message_id

        async def disconnect(self):
            pass

    provider._client = _Client()
    monkeypatch.setattr(history, "claude_user_uuid_after", lambda _s, _c: "U_EDIT")
    _stub_run(provider, monkeypatch)

    await provider.edit("x", cut_uuid="U_KEEP", restore_code=True)

    assert rewound["uid"] == "U_EDIT"


async def test_edit_no_restore_code_skips_rewind(monkeypatch):
    provider, _ = _capturing()
    provider._sdk_session_id = "SDK_ABC"
    provider._connected = True

    rewound: dict = {}

    class _Client:
        async def rewind_files(self, user_message_id):
            rewound["uid"] = user_message_id

        async def disconnect(self):
            pass

    provider._client = _Client()
    _stub_run(provider, monkeypatch)

    await provider.edit("x", cut_uuid="U_KEEP", restore_code=False)

    assert "uid" not in rewound


# --- capabilities -----------------------------------------------------------
def test_capabilities_advertise_edit():
    by_name = {p["name"]: p for p in capabilities.describe()["providers"]}
    assert by_name["claude"]["supports_edit"] is True
    assert by_name["claude"]["supports_code_restore"] is True
    assert by_name["codex"]["supports_edit"] is False
    assert by_name["codex"]["supports_code_restore"] is False


# --- history fork-point lookup ----------------------------------------------
def test_claude_user_uuid_after(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in [
                {"type": "user", "uuid": "U1"},
                {"type": "assistant", "uuid": "A1"},
                {"type": "user", "uuid": "U2"},
                {"type": "assistant", "uuid": "A2"},
            ]
        )
    )
    monkeypatch.setattr(history, "_claude_path", lambda _sid: path)

    assert history.claude_user_uuid_after("sid", "A1") == "U2"  # turn after the cut
    assert history.claude_user_uuid_after("sid", None) == "U1"  # first turn
    assert history.claude_user_uuid_after("sid", "A2") is None  # nothing after last


def test_claude_user_uuid_after_missing(monkeypatch):
    monkeypatch.setattr(history, "_claude_path", lambda _sid: None)
    assert history.claude_user_uuid_after("sid", "X") is None


# --- codex: edit unsupported -------------------------------------------------
async def test_codex_edit_reports_unsupported():
    provider, events = _capturing(CodexProvider)
    await provider.edit("x", cut_uuid=None)
    notices = [e for e in events if e["event"] == "session.notice"]
    assert notices and notices[0]["code"] == "edit_unsupported"
    assert [e for e in events if e["event"] == "session.result"]
