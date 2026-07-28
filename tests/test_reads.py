"""Unread watermarks: what the drawer's red dot is computed from."""
import json
import threading

import pytest

from coding_bridge import reads


@pytest.fixture
def clock(monkeypatch):
    """A controllable millisecond clock — watermarks are compared, not wall time."""

    class Clock:
        now = 1_000_000

        def tick(self, ms: int = 1_000) -> int:
            self.now += ms
            return self.now

    clk = Clock()
    monkeypatch.setattr(reads, "_now_ms", lambda: clk.now)
    return clk


def _summary(sid, updated_at, running=False, provider="claude"):
    return {
        "provider": provider,
        "session_id": sid,
        "updated_at": updated_at,
        "running": running,
    }


def _file(tmp_path):
    return json.loads((tmp_path / "reads.json").read_text())


def test_baseline_suppresses_pre_existing_sessions(tmp_path, clock):
    # Everything already on disk when tracking starts counts as read, otherwise
    # the first upgrade paints hundreds of red dots at once.
    sessions = [_summary("old", clock.now - 500_000)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False
    assert _file(tmp_path)["baseline"] == 1_000_000


def test_session_finished_after_baseline_is_unread(tmp_path, clock):
    reads.annotate(tmp_path, [])  # seed the baseline
    sessions = [_summary("new", clock.tick())]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is True


def test_running_session_is_never_unread(tmp_path, clock):
    reads.annotate(tmp_path, [])
    sessions = [_summary("live", clock.tick(), running=True)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False


def test_mark_clears_unread(tmp_path, clock):
    reads.annotate(tmp_path, [])
    updated_at = clock.tick()
    reads.mark(tmp_path, "claude", "s1", updated_at)
    sessions = [_summary("s1", updated_at)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False


def test_new_output_after_mark_is_unread_again(tmp_path, clock):
    # A watermark, not a boolean: reading then re-running must flip it back.
    reads.annotate(tmp_path, [])
    first = clock.tick()
    reads.mark(tmp_path, "claude", "s1", first)
    cleared = [_summary("s1", first)]
    reads.annotate(tmp_path, cleared)
    assert cleared[0]["unread"] is False

    # Only the watermark (not the baseline) can explain this one staying unread.
    sessions = [_summary("s1", first + 5_000)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is True


def test_output_appended_during_the_round_trip_stays_unread(tmp_path, clock):
    """The browser marks what it rendered, so a later append isn't swallowed."""
    reads.annotate(tmp_path, [])
    rendered = clock.tick()
    appended = rendered + 1
    clock.tick(60_000)  # the mark arrives long after the append landed
    reads.mark(tmp_path, "claude", "s1", rendered)
    sessions = [_summary("s1", appended)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is True


def test_future_mtime_is_still_clearable(tmp_path, clock):
    """Clock skew or a restored backup must not stick the dot on forever."""
    reads.annotate(tmp_path, [])
    future = clock.now + 3_600_000
    reads.mark(tmp_path, "claude", "s1", future)
    sessions = [_summary("s1", future)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False


def test_watermark_never_goes_backwards(tmp_path, clock):
    reads.annotate(tmp_path, [])
    newest = clock.tick(10_000)
    reads.mark(tmp_path, "claude", "s1", newest)
    reads.mark(tmp_path, "claude", "s1", newest - 5_000)  # a stale snapshot replays
    sessions = [_summary("s1", newest)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False


def test_providers_do_not_share_a_watermark(tmp_path, clock):
    """Two providers can mint the same local id; identity is the pair."""
    reads.annotate(tmp_path, [])
    updated_at = clock.tick()
    reads.mark(tmp_path, "claude", "dup", updated_at)
    sessions = [_summary("dup", updated_at, provider="codex")]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is True


def test_mark_preserves_baseline_and_other_sessions(tmp_path, clock):
    reads.annotate(tmp_path, [])
    reads.mark(tmp_path, "claude", "s1", clock.tick())
    reads.mark(tmp_path, "claude", "s2", clock.tick())
    data = _file(tmp_path)
    assert data["baseline"] == 1_000_000
    assert set(data["reads"]) == {"claude:s1", "claude:s2"}


def test_baseline_stays_a_floor_for_unmarked_sessions(tmp_path, clock):
    reads.annotate(tmp_path, [])
    reads.mark(tmp_path, "claude", "s1", clock.tick())  # must not lower the floor
    sessions = [_summary("untouched", clock.now - 500_000)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is False


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 201, None])
def test_unsafe_session_id_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        reads.mark(tmp_path, "claude", bad)
    # The id becomes a JSON key, so assert nothing unsafe was stored at all.
    assert not (tmp_path / "reads.json").exists() or not _file(tmp_path)["reads"]


@pytest.mark.parametrize("bad", ["../escape", "", None])
def test_unsafe_provider_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        reads.mark(tmp_path, bad, "s1")


def test_corrupt_file_is_reseeded(tmp_path, clock):
    (tmp_path / "reads.json").write_text("{ not json")
    sessions = [_summary("s1", clock.now - 500_000)]
    reads.annotate(tmp_path, sessions)
    # Reseeding puts the baseline at "now", so a corrupt file loses watermarks but
    # never floods the drawer.
    assert sessions[0]["unread"] is False
    assert _file(tmp_path)["baseline"] == 1_000_000


def test_undecodable_file_is_reseeded(tmp_path, clock):
    # A half-written file is invalid UTF-8, not merely invalid JSON.
    (tmp_path / "reads.json").write_bytes(b'{"reads": {"a": \xff\xfe}}')
    reads.mark(tmp_path, "claude", "s1", clock.tick())
    assert _file(tmp_path)["reads"] == {"claude:s1": 1_001_000}


def test_unreadable_file_never_wipes_watermarks(tmp_path, clock, monkeypatch):
    """A transient read error must abort, not silently reseed an empty file."""
    reads.mark(tmp_path, "claude", "s1", clock.tick())
    before = _file(tmp_path)

    real_open = open

    def flaky(path, *args, **kwargs):
        if str(path).endswith("reads.json"):
            raise PermissionError("locked by antivirus")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky)
    with pytest.raises(OSError):
        reads.mark(tmp_path, "claude", "s2", clock.tick())
    monkeypatch.undo()
    assert _file(tmp_path) == before


def test_annotate_survives_unwritable_config_dir(tmp_path, clock):
    unwritable = tmp_path / "nope"
    unwritable.write_text("i am a file, not a dir")
    sessions = [_summary("s1", clock.now)]
    reads.annotate(unwritable, sessions)  # must not raise
    # Fail closed: an unusable sidecar must not flood the drawer with red dots.
    assert sessions[0]["unread"] is False


def test_non_finite_numbers_do_not_break_annotation(tmp_path, clock):
    # json.load accepts Infinity/NaN; int() on them raises.
    (tmp_path / "reads.json").write_text('{"baseline": Infinity, "reads": {"claude:s1": NaN}}')
    sessions = [_summary("s1", clock.now)]
    reads.annotate(tmp_path, sessions)
    assert sessions[0]["unread"] is True


def test_eviction_drops_the_stalest_sessions(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(reads, "_MAX_ENTRIES", 3)
    for i in range(5):
        reads.mark(tmp_path, "claude", f"s{i}", clock.tick())
    # Watermarks hold each session's own updated_at, so the smallest are the stalest.
    assert set(_file(tmp_path)["reads"]) == {"claude:s2", "claude:s3", "claude:s4"}


def test_concurrent_marks_do_not_lose_a_watermark(tmp_path, clock):
    reads.annotate(tmp_path, [])
    base = clock.tick()
    threads = [
        threading.Thread(target=reads.mark, args=(tmp_path, "claude", f"s{i}", base + i))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_file(tmp_path)["reads"]) == 20
