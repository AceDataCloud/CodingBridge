"""Render an OS service unit for ``coding-bridge channels start``.

Pure rendering (no filesystem side effects) so it's unit-testable; the CLI
(:func:`coding_bridge.channels_cli.cmd_channels_install_service`) writes the file
and prints the activation command. We only ever generate a **user-scoped** unit
(no sudo / admin) and we never enable or start it — the operator runs the printed
activation command themselves, so nothing touches the system unasked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

_LABEL = "coding-bridge-channels"
_MAC_LABEL = "cloud.acedata.coding-bridge-channels"

_TOKEN_NOTE = (
    "If any instance uses token_env, a background service won't see your shell "
    "exports — switch that instance to token_file in channels.toml, or add the "
    "token(s) to the unit's environment before enabling."
)


@dataclass(frozen=True)
class ServicePlan:
    """A ready-to-write service unit + the commands to activate it."""

    kind: str  # "systemd" | "launchd" | "schtasks"
    path: Path
    content: str
    activate: list[str]
    notes: list[str] = field(default_factory=list)


def _validate(python_exe: str, config_dir: str) -> None:
    """Reject control chars so a path can't inject a second unit directive.

    ``python_exe``/``config_dir`` are the operator's own local paths, but a
    newline in one would let it smuggle an extra ``systemd`` line (or break the
    ``.cmd``), so we fail fast rather than emit a dangerous unit.
    """
    for label, value in (("python path", python_exe), ("config dir", config_dir)):
        if any(ord(c) < 0x20 for c in value):
            raise ValueError(f"{label} contains a control character; refusing to write a unit")


def _systemd(python_exe: str, config_dir: str) -> str:
    return (
        "[Unit]\n"
        "Description=Coding Bridge channels (WeChat / Telegram)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="CODING_BRIDGE_CONFIG_DIR={config_dir}"\n'
        f'ExecStart="{python_exe}" -m coding_bridge channels start\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _launchd(python_exe: str, config_dir: str) -> str:
    py = escape(python_exe)
    cfg = escape(config_dir)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key><string>{_MAC_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{py}</string>\n"
        "    <string>-m</string>\n"
        "    <string>coding_bridge</string>\n"
        "    <string>channels</string>\n"
        "    <string>start</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"    <key>CODING_BRIDGE_CONFIG_DIR</key><string>{cfg}</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _win_cmd(python_exe: str, config_dir: str) -> str:
    # CRLF + quoted paths so a space in the python path / config dir is safe.
    return (
        "@echo off\r\n"
        f'set "CODING_BRIDGE_CONFIG_DIR={config_dir}"\r\n'
        f'"{python_exe}" -m coding_bridge channels start\r\n'
    )


def build_service_plan(
    system: str, python_exe: str, config_dir: Path | str, home: Path | str
) -> ServicePlan:
    """Render the service unit for ``system`` ("linux" / "darwin" / "windows")."""
    cfg = str(config_dir)
    py = str(python_exe)
    home = Path(home)
    _validate(py, cfg)
    if system == "linux":
        path = home / ".config" / "systemd" / "user" / f"{_LABEL}.service"
        return ServicePlan(
            "systemd",
            path,
            _systemd(py, cfg),
            [
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {_LABEL}.service",
            ],
            [
                _TOKEN_NOTE,
                "To keep it running without an active login: "
                "`sudo loginctl enable-linger $USER`.",
            ],
        )
    if system == "darwin":
        path = home / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
        return ServicePlan(
            "launchd",
            path,
            _launchd(py, cfg),
            [f"launchctl load {path}"],
            [_TOKEN_NOTE],
        )
    if system == "windows":
        path = Path(cfg) / "run-channels.cmd"
        return ServicePlan(
            "schtasks",
            path,
            _win_cmd(py, cfg),
            [
                f'schtasks /create /tn "CodingBridgeChannels" /tr "{path}" '
                "/sc onlogon /rl limited /f",
            ],
            [
                _TOKEN_NOTE,
                "Runs at next logon. Start it now with "
                "`schtasks /run /tn CodingBridgeChannels`.",
            ],
        )
    raise ValueError(f"unsupported platform {system!r}")


__all__ = ["ServicePlan", "build_service_plan"]
