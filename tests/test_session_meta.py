"""Per-session settings sidecar: roundtrip, merge, and path-traversal guard."""
from coding_bridge import session_meta


def test_save_then_load_roundtrip(tmp_path):
    session_meta.save(
        tmp_path,
        "sid-1",
        cwd="/repo",
        model_selector="opus[1m]",
        permission_mode="plan",
        effort="high",
    )
    loaded = session_meta.load(tmp_path, "sid-1")
    assert loaded == {
        "version": 2,
        "cwd": "/repo",
        "model_selector": "opus[1m]",
        "permission_mode": "plan",
        "effort": "high",
    }


def test_save_merges_and_drops_none(tmp_path):
    session_meta.save(tmp_path, "sid-1", cwd="/repo", model_selector="opus")
    # A later turn only changes effort/mode; cwd/selector must survive the merge.
    session_meta.save(tmp_path, "sid-1", permission_mode="acceptEdits", effort=None)
    loaded = session_meta.load(tmp_path, "sid-1")
    assert loaded["cwd"] == "/repo"
    assert loaded["model_selector"] == "opus"
    assert loaded["permission_mode"] == "acceptEdits"
    assert "effort" not in loaded


def test_explicit_default_clears_a_saved_selector(tmp_path):
    session_meta.save(tmp_path, "sid-1", model_selector="opus[1m]")
    session_meta.save(tmp_path, "sid-1", model_selector=None)
    loaded = session_meta.load(tmp_path, "sid-1")
    assert loaded == {"version": 2}


def test_load_missing_is_empty(tmp_path):
    assert session_meta.load(tmp_path, "nope") == {}


def test_unsafe_id_is_ignored(tmp_path):
    session_meta.save(tmp_path, "../escape", cwd="/repo")
    assert session_meta.load(tmp_path, "../escape") == {}
    # Nothing was written outside the sessions dir.
    assert not (tmp_path.parent / "escape.json").exists()


def test_ignores_unknown_fields(tmp_path):
    session_meta.save(tmp_path, "sid-1", cwd="/repo", secret="x")
    assert session_meta.load(tmp_path, "sid-1") == {"version": 2, "cwd": "/repo"}


def test_legacy_resolved_model_is_not_promoted_to_selector(tmp_path):
    from coding_bridge import store

    store.save(
        tmp_path / "sessions" / "sid-1.json",
        {"cwd": "/repo", "model": "claude-opus-5", "permission_mode": "plan"},
    )
    legacy = session_meta.load(tmp_path, "sid-1")
    assert legacy["model"] == "claude-opus-5"
    assert "model_selector" not in legacy

    session_meta.save(tmp_path, "sid-1", effort="high")
    migrated = session_meta.load(tmp_path, "sid-1")
    assert migrated == {
        "version": 2,
        "cwd": "/repo",
        "permission_mode": "plan",
        "effort": "high",
    }
