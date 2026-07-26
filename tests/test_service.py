"""Tests for `coding-bridge service` — unit rendering (pure) + lifecycle argv."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_bridge.service import build_service_plan, manager_argv

# ---------- rendering (pure, no filesystem) ----------------------------------


def test_systemd_plan(tmp_path):
    plan = build_service_plan("linux", "/usr/bin/python3", "/home/u/.ace-bridge", tmp_path)
    assert plan.kind == "systemd"
    assert plan.label == "coding-bridge.service"
    assert plan.path == tmp_path / ".config/systemd/user/coding-bridge.service"
    # main daemon, NOT `channels start`
    assert 'ExecStart="/usr/bin/python3" -m coding_bridge run' in plan.content
    assert "channels start" not in plan.content
    assert 'Environment="CODING_BRIDGE_CONFIG_DIR=/home/u/.ace-bridge"' in plan.content
    assert "WantedBy=default.target" in plan.content


def test_launchd_plan(tmp_path):
    plan = build_service_plan("darwin", "/usr/bin/python3", "/Users/u/.ace-bridge", tmp_path)
    assert plan.kind == "launchd"
    assert plan.label == "cloud.acedata.coding-bridge"
    assert plan.path == tmp_path / "Library/LaunchAgents/cloud.acedata.coding-bridge.plist"
    assert "<string>run</string>" in plan.content
    assert "<string>/usr/bin/python3</string>" in plan.content
    assert "<string>/Users/u/.ace-bridge</string>" in plan.content


def test_windows_plan(tmp_path):
    plan = build_service_plan("windows", r"C:\Py\python.exe", r"C:\Users\u\.ace-bridge", tmp_path)
    assert plan.kind == "schtasks"
    assert plan.label == "CodingBridge"
    assert plan.path == Path(r"C:\Users\u\.ace-bridge") / "run-daemon.cmd"
    assert '"C:\\Py\\python.exe" -m coding_bridge run' in plan.content
    assert 'set "CODING_BRIDGE_CONFIG_DIR=C:\\Users\\u\\.ace-bridge"' in plan.content


def test_unsupported_platform_raises(tmp_path):
    with pytest.raises(ValueError):
        build_service_plan("plan9", "/py", "/cfg", tmp_path)


def test_launchd_escapes_xml_special_chars(tmp_path):
    plan = build_service_plan("darwin", "/opt/py&x/python", "/cfg", tmp_path)
    assert "<string>/opt/py&amp;x/python</string>" in plan.content
    assert "/opt/py&x/python</string>" not in plan.content


def test_all_plans_carry_pair_note(tmp_path):
    for sysname in ("linux", "darwin", "windows"):
        plan = build_service_plan(sysname, "/py", "/cfg", tmp_path)
        assert any("coding-bridge pair" in n for n in plan.notes)


def test_rejects_control_char_in_path(tmp_path):
    with pytest.raises(ValueError):
        build_service_plan("linux", "/usr/bin/py\nExecStart=evil", "/cfg", tmp_path)
    with pytest.raises(ValueError):
        build_service_plan("linux", "/py", "/cfg\nRestart=no", tmp_path)


# ---------- lifecycle argv (pure — builds commands, runs nothing) ------------


def _plan(system, tmp_path):
    return build_service_plan(system, "/py", "/cfg", tmp_path)


def test_systemd_lifecycle_argv(tmp_path):
    plan = _plan("linux", tmp_path)
    assert manager_argv("linux", "install", plan) == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "coding-bridge.service"],
    ]
    assert manager_argv("linux", "start", plan) == [
        ["systemctl", "--user", "start", "coding-bridge.service"]
    ]
    assert manager_argv("linux", "stop", plan) == [
        ["systemctl", "--user", "stop", "coding-bridge.service"]
    ]
    assert manager_argv("linux", "uninstall", plan) == [
        ["systemctl", "--user", "disable", "--now", "coding-bridge.service"]
    ]


def test_launchd_lifecycle_argv(tmp_path):
    plan = _plan("darwin", tmp_path)
    assert manager_argv("darwin", "install", plan, uid=501) == [
        ["launchctl", "bootstrap", "gui/501", str(plan.path)]
    ]
    assert manager_argv("darwin", "stop", plan, uid=501) == [
        ["launchctl", "bootout", "gui/501/cloud.acedata.coding-bridge"]
    ]


def test_launchd_requires_uid(tmp_path):
    plan = _plan("darwin", tmp_path)
    with pytest.raises(ValueError):
        manager_argv("darwin", "install", plan)


def test_windows_lifecycle_argv(tmp_path):
    plan = _plan("windows", tmp_path)
    install = manager_argv("windows", "install", plan)
    assert install[0][:4] == ["schtasks", "/create", "/tn", "CodingBridge"]
    # /tr value must carry its own quotes so a spaced profile path survives.
    tr_idx = install[0].index("/tr") + 1
    assert install[0][tr_idx] == f'"{plan.path}"'
    assert install[1] == ["schtasks", "/run", "/tn", "CodingBridge"]
    assert manager_argv("windows", "uninstall", plan) == [
        ["schtasks", "/end", "/tn", "CodingBridge"],
        ["schtasks", "/delete", "/tn", "CodingBridge", "/f"],
    ]


def test_unknown_action_raises(tmp_path):
    plan = _plan("linux", tmp_path)
    with pytest.raises(ValueError):
        manager_argv("linux", "frobnicate", plan)


# ---------- CLI guardrails (install refuses when not paired) ------------------


def test_install_refuses_when_not_paired(tmp_path, capsys, monkeypatch):
    from coding_bridge import service_cli
    from coding_bridge.config import Settings

    settings = Settings(config_dir=tmp_path)  # no credentials.json → not paired
    # Guard against any accidental manager exec.
    monkeypatch.setattr(service_cli.subprocess, "run", _boom)
    rc = service_cli.cmd_service_install(settings, force=False)
    assert rc == 1
    assert "Not paired" in capsys.readouterr().err


def test_install_writes_unit_and_starts_when_paired(tmp_path, monkeypatch):
    from coding_bridge import service_cli, store
    from coding_bridge.config import Settings

    settings = Settings(config_dir=tmp_path / "cfg")
    store.save(settings.credentials_path, {"node_token": "tok"})
    # Keep the unit file inside tmp_path (it derives from Path.home(), not cfg).
    monkeypatch.setattr(service_cli.Path, "home", classmethod(lambda cls: tmp_path))

    calls: list[list[str]] = []

    def _fake_run(cmd, check=False):  # noqa: ARG001
        calls.append(cmd)
        return _Ok()

    monkeypatch.setattr(service_cli.subprocess, "run", _fake_run)
    rc = service_cli.cmd_service_install(settings, force=False)
    assert rc == 0
    plan = service_cli._plan(settings)
    assert plan.path.exists()  # unit file written
    assert calls  # manager was invoked to enable/start


class _Ok:
    returncode = 0


def test_darwin_install_precleans_before_bootstrap(tmp_path, monkeypatch):
    """On macOS, install must bootout an already-loaded label before bootstrap,
    else `--force` reinstall errors and silently leaves the old daemon running."""
    from coding_bridge import service_cli, store
    from coding_bridge.config import Settings

    settings = Settings(config_dir=tmp_path / "cfg")
    store.save(settings.credentials_path, {"node_token": "tok"})
    monkeypatch.setattr(service_cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service_cli, "_system", lambda: "darwin")
    monkeypatch.setattr(service_cli, "_uid", lambda: 501)

    calls: list[list[str]] = []

    def _fake_run(cmd, check=False, capture_output=False):  # noqa: ARG001
        calls.append(cmd)
        return _Ok()

    monkeypatch.setattr(service_cli.subprocess, "run", _fake_run)
    rc = service_cli.cmd_service_install(settings, force=True)
    assert rc == 0
    kinds = [c[1] for c in calls]  # launchctl <verb> ...
    assert "bootout" in kinds
    assert kinds.index("bootout") < kinds.index("bootstrap")


def _boom(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("subprocess.run should not be called here")
