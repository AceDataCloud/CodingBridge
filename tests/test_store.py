from coding_bridge_agent import store


def test_roundtrip(tmp_path):
    path = tmp_path / "nested" / "credentials.json"
    assert store.load(path) is None
    store.save(path, {"node_token": "node_abc", "node_name": "laptop"})
    loaded = store.load(path)
    assert loaded["node_token"] == "node_abc"
    assert loaded["node_name"] == "laptop"


def test_clear(tmp_path):
    path = tmp_path / "credentials.json"
    store.save(path, {"node_token": "node_abc"})
    assert store.clear(path) is True
    assert store.clear(path) is False
    assert store.load(path) is None


def test_load_corrupt_returns_none(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{ not json", encoding="utf-8")
    assert store.load(path) is None
