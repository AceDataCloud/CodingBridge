from coding_bridge_agent import capabilities


def test_describe_lists_both_providers():
    desc = capabilities.describe()
    names = [p["name"] for p in desc["providers"]]
    assert names == ["claude", "codex"]


def test_describe_marks_availability_from_which(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: "/usr/bin/" + cli)
    desc = capabilities.describe()
    assert all(p["available"] for p in desc["providers"])
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: None)
    desc = capabilities.describe()
    assert all(not p["available"] for p in desc["providers"])


def test_claude_has_max_effort_codex_does_not():
    desc = capabilities.describe()
    by_name = {p["name"]: p for p in desc["providers"]}
    assert "max" in by_name["claude"]["efforts"]
    assert "max" not in by_name["codex"]["efforts"]
    # Both expose a "default" sentinel ("") tier.
    assert "" in by_name["claude"]["efforts"]
    assert "" in by_name["codex"]["efforts"]


def test_models_are_value_label_pairs():
    desc = capabilities.describe()
    for provider in desc["providers"]:
        assert provider["models"], provider["name"]
        for model in provider["models"]:
            assert set(model) == {"value", "label"}
        assert provider["allow_custom_model"] is True


def test_permission_modes_present():
    desc = capabilities.describe()
    for provider in desc["providers"]:
        assert provider["permission_modes"] == [
            "default",
            "acceptEdits",
            "plan",
            "bypassPermissions",
        ]
