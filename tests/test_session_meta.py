"""Per-session settings sidecar: roundtrip, merge, and path-traversal guard."""
from coding_bridge import session_meta


def test_save_then_load_roundtrip(tmp_path):
    session_meta.save(
        tmp_path, "sid-1", cwd="/repo", model="opus", permission_mode="plan", effort="high"
    )
    loaded = session_meta.load(tmp_path, "sid-1")
    assert loaded == {
        "cwd": "/repo",
        "model": "opus",
        "permission_mode": "plan",
        "effort": "high",
    }


def test_save_merges_and_drops_none(tmp_path):
    session_meta.save(tmp_path, "sid-1", cwd="/repo", model="opus")
    # A later turn only changes effort/mode; cwd/model must survive the merge.
    session_meta.save(tmp_path, "sid-1", permission_mode="acceptEdits", effort=None, model=None)
    loaded = session_meta.load(tmp_path, "sid-1")
    assert loaded["cwd"] == "/repo"
    assert loaded["model"] == "opus"
    assert loaded["permission_mode"] == "acceptEdits"
    assert "effort" not in loaded


def test_load_missing_is_empty(tmp_path):
    assert session_meta.load(tmp_path, "nope") == {}


def test_unsafe_id_is_ignored(tmp_path):
    session_meta.save(tmp_path, "../escape", cwd="/repo")
    assert session_meta.load(tmp_path, "../escape") == {}
    # Nothing was written outside the sessions dir.
    assert not (tmp_path.parent / "escape.json").exists()


def test_ignores_unknown_fields(tmp_path):
    session_meta.save(tmp_path, "sid-1", cwd="/repo", secret="x")
    assert session_meta.load(tmp_path, "sid-1") == {"cwd": "/repo"}
