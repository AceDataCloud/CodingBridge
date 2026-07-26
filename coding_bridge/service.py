"""Render + manage an OS service that runs the main ``coding-bridge`` daemon.

The unit **rendering** is pure (no filesystem / no subprocess) so it stays
unit-testable, exactly like :mod:`coding_bridge.channels.service` (which does the
same for the ``channels start`` chat bridge). This module differs in two ways:

* the service runs the **main daemon** — ``python -m coding_bridge run`` — so a
  browser/Nexior session can reach the node across logout / reboot;
* it also exposes :func:`manager_argv`, the ordered manager commands for each
  lifecycle action (start / stop / status / uninstall), so the CLI can offer a
  full ``coding-bridge service {install,start,stop,status,uninstall}`` that
  actually drives systemd / launchd / Task Scheduler.

Everything is **user-scoped** (``systemd --user`` / a LaunchAgent /
a per-user scheduled task) and never root/SYSTEM: the daemon executes the
provider CLIs with the user's login state (nvm PATH, Claude/Codex auth), which a
system service would not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

_LABEL = "coding-bridge"
_MAC_LABEL = "cloud.acedata.coding-bridge"
_WIN_TASK = "CodingBridge"

# The daemon needs a paired node token and the user's login state; a service
# can't pair interactively, and two daemons on one token fight over the relay.
_PAIR_NOTE = (
    "Pair once before starting the service: `coding-bridge pair`. The service "
    "can't pair interactively."
)
_LOCK_NOTE = (
    "Don't also run `coding-bridge up` in a terminal while the service is "
    "active — two daemons share one node token and tear down every session."
)


@dataclass(frozen=True)
class ServicePlan:
    """A ready-to-write service unit + how to identify/activate it."""

    kind: str  # "systemd" | "launchd" | "schtasks"
    label: str  # unit/label/task name used by the manager
    path: Path  # where the unit file is written
    content: str
    activate: list[str]  # human-facing activation hint(s)
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
        "Description=Coding Bridge daemon (drive Claude Code / Codex from the web)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="CODING_BRIDGE_CONFIG_DIR={config_dir}"\n'
        f'ExecStart="{python_exe}" -m coding_bridge run\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # exit 1 = not paired / already running — a config problem, not a crash;
        # treat it as clean so we don't restart-loop on it.
        "SuccessExitStatus=0 1\n"
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
        "    <string>run</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"    <key>CODING_BRIDGE_CONFIG_DIR</key><string>{cfg}</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n"
        # Restart on crash but not after a clean stop; back off so a bad config
        # (exit 1) doesn't spin.
        "  <key>KeepAlive</key>\n"
        "  <dict><key>SuccessfulExit</key><false/></dict>\n"
        "  <key>ThrottleInterval</key><integer>10</integer>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _win_cmd(python_exe: str, config_dir: str) -> str:
    # CRLF + quoted paths so a space in the python path / config dir is safe.
    return (
        "@echo off\r\n"
        f'set "CODING_BRIDGE_CONFIG_DIR={config_dir}"\r\n'
        f'"{python_exe}" -m coding_bridge run\r\n'
    )


def build_service_plan(
    system: str, python_exe: str, config_dir: Path | str, home: Path | str
) -> ServicePlan:
    """Render the daemon service unit for ``system`` (linux/darwin/windows)."""
    cfg = str(config_dir)
    py = str(python_exe)
    home = Path(home)
    _validate(py, cfg)
    notes = [_PAIR_NOTE, _LOCK_NOTE]
    if system == "linux":
        path = home / ".config" / "systemd" / "user" / f"{_LABEL}.service"
        return ServicePlan(
            "systemd",
            f"{_LABEL}.service",
            path,
            _systemd(py, cfg),
            [
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {_LABEL}.service",
            ],
            [
                *notes,
                "To keep it running without an active login: "
                "`sudo loginctl enable-linger $USER`.",
            ],
        )
    if system == "darwin":
        path = home / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
        return ServicePlan(
            "launchd",
            _MAC_LABEL,
            path,
            _launchd(py, cfg),
            [f"launchctl bootstrap gui/$(id -u) {path}"],
            notes,
        )
    if system == "windows":
        path = Path(cfg) / "run-daemon.cmd"
        return ServicePlan(
            "schtasks",
            _WIN_TASK,
            path,
            _win_cmd(py, cfg),
            [
                f'schtasks /create /tn "{_WIN_TASK}" /tr "{path}" '
                "/sc onlogon /rl limited /f",
                f"schtasks /run /tn {_WIN_TASK}",
            ],
            notes,
        )
    raise ValueError(f"unsupported platform {system!r}")


def manager_argv(
    system: str, action: str, plan: ServicePlan, *, uid: int | None = None
) -> list[list[str]]:
    """Ordered manager commands for ``action`` on this plan.

    ``install`` returns the commands to run **after** the CLI has written the
    unit file; ``uninstall`` returns the commands to run **before** it removes
    the file. Pure — builds argv only, runs nothing. ``uid`` (POSIX
    ``os.getuid()``) is required for launchd's ``gui/<uid>`` domain target.
    """
    label = plan.label
    if system == "linux":
        base = ["systemctl", "--user"]
        if action == "install":
            return [[*base, "daemon-reload"], [*base, "enable", "--now", label]]
        if action == "start":
            return [[*base, "start", label]]
        if action == "stop":
            return [[*base, "stop", label]]
        if action == "status":
            return [[*base, "status", "--no-pager", label]]
        if action == "uninstall":
            return [[*base, "disable", "--now", label]]
    elif system == "darwin":
        if uid is None:
            raise ValueError("launchd lifecycle needs a uid")
        target = f"gui/{uid}"
        svc_target = f"{target}/{label}"
        if action == "install":
            return [["launchctl", "bootstrap", target, str(plan.path)]]
        if action == "start":
            # bootstrap loads (and RunAtLoad starts) a plist not currently loaded.
            return [["launchctl", "bootstrap", target, str(plan.path)]]
        if action == "stop":
            # KeepAlive would relaunch on a plain `stop`, so bootout (unload)
            # instead; the plist stays on disk for a later start.
            return [["launchctl", "bootout", svc_target]]
        if action == "status":
            return [["launchctl", "print", svc_target]]
        if action == "uninstall":
            return [["launchctl", "bootout", svc_target]]
    elif system == "windows":
        if action == "install":
            # /tr must carry its own quotes: Task Scheduler stores the string
            # verbatim and splits the action on the first space at run time, so
            # an unquoted `C:\Users\John Doe\...` would launch `C:\Users\John`.
            tr = f'"{plan.path}"'
            return [
                [
                    "schtasks", "/create", "/tn", label, "/tr", tr,
                    "/sc", "onlogon", "/rl", "limited", "/f",
                ],
                ["schtasks", "/run", "/tn", label],
            ]
        if action == "start":
            return [["schtasks", "/run", "/tn", label]]
        if action == "stop":
            return [["schtasks", "/end", "/tn", label]]
        if action == "status":
            return [["schtasks", "/query", "/tn", label, "/v", "/fo", "LIST"]]
        if action == "uninstall":
            return [["schtasks", "/end", "/tn", label], ["schtasks", "/delete", "/tn", label, "/f"]]
    else:
        raise ValueError(f"unsupported platform {system!r}")
    raise ValueError(f"unsupported action {action!r}")


__all__ = ["ServicePlan", "build_service_plan", "manager_argv"]
