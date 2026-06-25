import json

from coding_bridge import capabilities
from coding_bridge.config import Settings


def test_describe_lists_providers():
    desc = capabilities.describe()
    names = [p["name"] for p in desc["providers"]]
    assert names == ["claude", "codex", "copilot"]


def _write_models_cache(tmp_path, monkeypatch):
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.4",
                        "display_name": "GPT-5.4",
                        "visibility": "list",
                        "priority": 16,
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                    },
                    {
                        "slug": "gpt-5.5",
                        "display_name": "GPT-5.5",
                        "visibility": "list",
                        "priority": 9,
                        "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "xhigh"}],
                    },
                    {
                        "slug": "codex-auto-review",
                        "display_name": "Auto Review",
                        "visibility": "hide",
                        "priority": 1,
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(capabilities, "CODEX_MODELS_CACHE", cache)
    return cache


def test_codex_models_read_from_host_cache(monkeypatch, tmp_path):
    _write_models_cache(tmp_path, monkeypatch)
    models = capabilities._codex_models()
    # Sorted by codex's priority; the `hide` model is excluded; slug+display kept.
    assert models == [
        {"value": "gpt-5.5", "label": "GPT-5.5"},
        {"value": "gpt-5.4", "label": "GPT-5.4"},
    ]
    # Efforts are the union of the listed models' supported levels, canonical order.
    assert capabilities._codex_efforts() == ["", "low", "medium", "high", "xhigh"]


def test_codex_catalog_falls_back_to_config_without_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(capabilities, "CODEX_MODELS_CACHE", tmp_path / "nope.json")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'model = "gpt-6-codex"\n'
        'model_reasoning_effort = "insane"\n'
        "[tui]\n"
        'model = "ignored"\n'
    )
    monkeypatch.setattr(capabilities, "CODEX_CONFIG", cfg)
    models = [m["value"] for m in capabilities._codex_models()]
    assert models[0] == "gpt-6-codex"  # configured model surfaces first, no node release
    assert "insane" in capabilities._codex_efforts()  # top-level only; [tui] is ignored


def test_codex_config_value_missing_file_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(capabilities, "CODEX_CONFIG", tmp_path / "nope.toml")
    assert capabilities._codex_config_value("model") is None


def test_describe_marks_availability_from_which(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: "/usr/bin/" + cli)
    desc = capabilities.describe()
    assert all(p["available"] for p in desc["providers"])
    # Not on PATH and not in any known install dir → genuinely unavailable.
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: None)
    monkeypatch.setattr(capabilities, "_candidate_cli_paths", lambda cli: [])
    desc = capabilities.describe()
    assert all(not p["available"] for p in desc["providers"])


def test_resolve_cli_prefers_settings_override(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: None)
    settings = Settings(claude_path=str(binary))
    assert capabilities.resolve_cli("claude", settings) == str(binary)


def test_resolve_cli_falls_back_to_known_dirs_when_path_misses(monkeypatch, tmp_path):
    # Simulate the nvm case: not on PATH, but present in a known install dir.
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(capabilities.shutil, "which", lambda cli: None)
    monkeypatch.setattr(capabilities, "_candidate_cli_paths", lambda cli: [str(binary)])
    assert capabilities.resolve_cli("claude") == str(binary)
    # A non-existent candidate resolves to None (the binary really is absent).
    monkeypatch.setattr(capabilities, "_candidate_cli_paths", lambda cli: [str(tmp_path / "nope")])
    assert capabilities.resolve_cli("codex") is None


def test_ensure_clis_on_path_prepends_resolved_dir(monkeypatch, tmp_path):
    bindir = tmp_path / "node" / "bin"
    bindir.mkdir(parents=True)
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        capabilities,
        "resolve_cli",
        lambda cli, settings=None: str(claude) if cli == "claude" else None,
    )
    added = capabilities.ensure_clis_on_path(Settings())
    assert str(bindir) in added
    assert capabilities.os.environ["PATH"].split(":")[0] == str(bindir)


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

