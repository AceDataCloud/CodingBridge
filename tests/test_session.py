"""Session resilience: structured errors, self-healing reset, bounded retry."""

from coding_bridge.config import Settings
from coding_bridge.protocol import Event, event_payload
from coding_bridge.session import Session


class _Crash(Exception):
    """Stand-in for the SDK's ProcessError (carries an ``exit_code``)."""

    def __init__(self, msg: str = "Command failed", exit_code: int | None = None):
        super().__init__(msg)
        self.exit_code = exit_code


class _FakeProvider:
    name = "fake"

    def __init__(self, emit, script: list):
        self._emit = emit
        self._script = script
        self.calls = 0
        self.closed = 0

    async def start(self, prompt, **_kw):
        await self._run()

    async def send(self, prompt, **_kw):
        await self._run()

    async def _run(self):
        step = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if step == "ok":
            return
        if isinstance(step, tuple):  # ("emit", exc): stream output, then crash
            await self._emit(event_payload(Event.SESSION_TEXT, "s1", text="partial"))
            raise step[1]
        raise step

    async def interrupt(self):
        pass

    async def aclose(self):
        self.closed += 1


def _make(script: list, settings: Settings | None = None):
    events: list[dict] = []
    holder: dict = {}

    async def emit(payload):
        events.append(payload)

    async def ask(*_a):
        return "deny"

    def factory(_name, _sid, emit_fn, _ask):
        prov = _FakeProvider(emit_fn, script)
        holder["provider"] = prov
        return prov

    sess = Session(
        "s1", factory, emit, settings or Settings(turn_retry_backoff=0.0),
        cwd="/tmp", model="m", permission_mode="default", provider="fake",
    )
    return sess, events, holder


def _errors(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event") == Event.SESSION_ERROR]


async def test_clean_crash_is_retried_then_succeeds():
    sess, events, holder = _make([_Crash(exit_code=3), "ok"])
    await sess.start("hi")
    await sess._task
    prov = holder["provider"]
    assert prov.calls == 2        # retried once
    assert prov.closed == 1       # provider reset between attempts
    assert _errors(events) == []  # user never saw the transient crash
    assert sess.status == "idle"


async def test_crash_after_output_is_not_retried():
    sess, events, holder = _make([("emit", _Crash(exit_code=3))])
    await sess.start("hi")
    await sess._task
    prov = holder["provider"]
    assert prov.calls == 1  # replaying could duplicate the streamed work
    err = _errors(events)
    assert len(err) == 1
    assert err[0]["code"] == "process_crashed"
    assert err[0]["exit_code"] == 3
    assert prov.closed == 1  # fatal → provider reset so next turn reconnects


async def test_generic_error_surfaces_without_reset_or_retry():
    sess, events, holder = _make([ValueError("oops")])
    await sess.start("hi")
    await sess._task
    prov = holder["provider"]
    assert prov.calls == 1
    assert prov.closed == 0  # non-fatal: a healthy client must not be dropped
    err = _errors(events)
    assert len(err) == 1
    assert err[0]["code"] == "provider_error"
    assert err[0]["exit_code"] is None


async def test_started_event_carries_effort_and_permission_mode():
    settings = Settings(turn_retry_backoff=0.0)
    events: list[dict] = []

    async def emit(payload):
        events.append(payload)

    async def ask(*_a):
        return "deny"

    def factory(_name, _sid, emit_fn, _ask):
        class _P:
            name = "fake"

            async def start(self, prompt, **_kw):
                return None

            async def aclose(self):
                return None

        return _P()

    sess = Session(
        "s1", factory, emit, settings,
        cwd="/tmp", model="m", permission_mode="plan", provider="fake", effort="high",
    )
    await sess.start("hi")
    await sess._task
    started = [e for e in events if e.get("event") == Event.SESSION_STARTED]
    assert started and started[0]["permission_mode"] == "plan"
    assert started[0]["effort"] == "high"
    info = sess.info()
    assert info["permission_mode"] == "plan" and info["effort"] == "high"


async def test_result_with_sdk_session_id_persists_settings(tmp_path):
    from coding_bridge import session_meta

    settings = Settings(turn_retry_backoff=0.0, config_dir=tmp_path)

    async def emit(payload):
        return None

    def factory(_name, _sid, emit_fn, _ask):
        class _P:
            name = "fake"

            async def start(self, prompt, **_kw):
                # A turn's result carries the on-disk transcript id.
                await emit_fn(event_payload(Event.SESSION_RESULT, "s1", sdk_session_id="disk-9"))

            async def aclose(self):
                return None

        return _P()

    async def ask(*_a):
        return "deny"

    sess = Session(
        "s1", factory, emit, settings,
        cwd="/repo", model="opus", permission_mode="acceptEdits", provider="claude", effort="high",
    )
    await sess.start("hi")
    await sess._task
    saved = session_meta.load(tmp_path, "disk-9")
    assert saved == {
        "cwd": "/repo",
        "model": "opus",
        "permission_mode": "acceptEdits",
        "effort": "high",
        "provider": "claude",
    }


async def test_retry_limit_is_respected():
    sess, events, holder = _make(
        [_Crash(exit_code=3), _Crash(exit_code=3), _Crash(exit_code=3)],
        Settings(turn_retry_limit=1, turn_retry_backoff=0.0),
    )
    await sess.start("hi")
    await sess._task
    prov = holder["provider"]
    assert prov.calls == 2  # original + 1 retry, then give up
    assert len(_errors(events)) == 1
