"""Tests for `channels install-service` unit rendering (pure, no filesystem)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_bridge.channels.service import build_service_plan


def test_systemd_plan(tmp_path):
    plan = build_service_plan("linux", "/usr/bin/python3", "/home/u/.ace-bridge", tmp_path)
    assert plan.kind == "systemd"
    assert plan.path == tmp_path / ".config/systemd/user/coding-bridge-channels.service"
    assert 'ExecStart="/usr/bin/python3" -m coding_bridge channels start' in plan.content
    assert 'Environment="CODING_BRIDGE_CONFIG_DIR=/home/u/.ace-bridge"' in plan.content
    assert "WantedBy=default.target" in plan.content
    assert any("systemctl --user enable --now" in c for c in plan.activate)


def test_launchd_plan(tmp_path):
    plan = build_service_plan("darwin", "/usr/bin/python3", "/Users/u/.ace-bridge", tmp_path)
    assert plan.kind == "launchd"
    assert (
        plan.path
        == tmp_path / "Library/LaunchAgents/cloud.acedata.coding-bridge-channels.plist"
    )
    assert "<string>coding_bridge</string>" in plan.content
    assert "<string>/usr/bin/python3</string>" in plan.content
    assert "<string>/Users/u/.ace-bridge</string>" in plan.content
    assert any("launchctl load" in c for c in plan.activate)


def test_windows_plan(tmp_path):
    plan = build_service_plan(
        "windows", r"C:\Py\python.exe", r"C:\Users\u\.ace-bridge", tmp_path
    )
    assert plan.kind == "schtasks"
    assert plan.path == Path(r"C:\Users\u\.ace-bridge") / "run-channels.cmd"
    assert '"C:\\Py\\python.exe" -m coding_bridge channels start' in plan.content
    assert 'set "CODING_BRIDGE_CONFIG_DIR=C:\\Users\\u\\.ace-bridge"' in plan.content
    assert any(
        "schtasks /create" in c and "CodingBridgeChannels" in c for c in plan.activate
    )


def test_unsupported_platform_raises(tmp_path):
    with pytest.raises(ValueError):
        build_service_plan("plan9", "/py", "/cfg", tmp_path)


def test_launchd_escapes_xml_special_chars(tmp_path):
    plan = build_service_plan("darwin", "/opt/py&x/python", "/cfg", tmp_path)
    assert "<string>/opt/py&amp;x/python</string>" in plan.content
    assert "/opt/py&x/python</string>" not in plan.content  # raw & would break the plist


def test_all_plans_carry_token_note(tmp_path):
    for sysname in ("linux", "darwin", "windows"):
        plan = build_service_plan(sysname, "/py", "/cfg", tmp_path)
        assert any("token_env" in n for n in plan.notes)


def test_rejects_control_char_in_path(tmp_path):
    # a newline could smuggle a second systemd directive / break the .cmd
    with pytest.raises(ValueError):
        build_service_plan("linux", "/usr/bin/py\nExecStart=evil", "/cfg", tmp_path)
    with pytest.raises(ValueError):
        build_service_plan("linux", "/py", "/cfg\nRestart=no", tmp_path)
