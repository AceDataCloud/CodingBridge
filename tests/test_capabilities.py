from coding_bridge_agent import capabilities
from coding_bridge_agent.config import Settings


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


def test_normalize_commands_maps_fields_and_skips_junk():
    info = {
        "commands": [
            {
                "name": "usage",
                "description": "cost",
                "argumentHint": "",
                "aliases": ["cost", "stats"],
            },
            {"name": "context", "description": "ctx", "argumentHint": "[x]"},
            "not-a-dict",
            {"description": "no name"},
        ]
    }
    cmds = capabilities.normalize_commands(info)
    assert [c["name"] for c in cmds] == ["usage", "context"]
    assert cmds[0]["aliases"] == ["cost", "stats"]
    assert cmds[1]["argument_hint"] == "[x]"


def test_normalize_commands_handles_none():
    assert capabilities.normalize_commands(None) == []
    assert capabilities.normalize_commands({}) == []


def test_command_name_set_lowercases_names_and_aliases():
    cmds = [{"name": "Usage", "aliases": ["Cost", "Stats"]}, {"name": "Context"}]
    assert capabilities.command_name_set(cmds) == {"usage", "cost", "stats", "context"}


def test_codex_commands_reads_prompts_dir(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "deploy.md").write_text("do deploy")
    (prompts / "review.md").write_text("review")
    (prompts / "ignore.txt").write_text("not markdown")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    names = sorted(c["name"] for c in capabilities._codex_commands())
    assert names == ["deploy", "review"]


def test_codex_commands_empty_without_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert capabilities._codex_commands() == []


async def test_describe_detailed_attaches_commands(monkeypatch):
    async def fake_claude(_settings):
        return [{"name": "context", "description": "", "argument_hint": "", "aliases": []}]

    monkeypatch.setattr(capabilities, "_claude_commands", fake_claude)
    monkeypatch.setattr(capabilities, "_codex_commands", lambda: [])
    desc = await capabilities.describe_detailed(Settings())
    by_name = {p["name"]: p for p in desc["providers"]}
    assert by_name["claude"]["commands"][0]["name"] == "context"
    assert by_name["codex"]["commands"] == []

