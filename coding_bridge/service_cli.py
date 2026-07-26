"""CLI plumbing for the ``coding-bridge service`` subcommand group.

Full-lifecycle management of the main daemon as a **user-scoped** OS service:

* ``install [--force]`` - write the unit + register/enable it with the platform
  manager (systemd --user / launchd / Task Scheduler) and start it now.
* ``start`` / ``stop`` / ``status`` - drive the manager.
* ``uninstall`` - stop, deregister, and remove the unit file.

Unit rendering lives in :mod:`coding_bridge.service` (pure, testable); this file
holds the filesystem writes and the ``subprocess`` calls to the manager, so the
service module stays side-effect-free.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform as _platform
import subprocess
import sys
from pathlib import Path

from . import store
from .config import Settings
from .service import ServicePlan, build_service_plan, manager_argv


def _system() -> str:
    return _platform.system().lower()


def _plan(settings: Settings) -> ServicePlan | None:
    try:
        return build_service_plan(_system(), sys.executable, settings.config_dir, Path.home())
    except ValueError:
        return None


def _run(cmds: list[list[str]]) -> int:
    """Run manager commands in order; stop at the first failure and return it.

    A missing manager (systemctl/launchctl/schtasks not on PATH) is reported
    clearly and mapped to exit 2 ("environment problem", not "try again").
    """
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, check=False)  # noqa: S603 - fixed argv, no shell
        except FileNotFoundError:
            print(
                f"'{cmd[0]}' not found - this platform's service manager is "
                "unavailable. See docs/deploy/ for manual templates.",
                file=sys.stderr,
            )
            return 2
        if proc.returncode != 0:
            return proc.returncode
    return 0


def _launchd_preclean(plan: ServicePlan, uid: int | None) -> None:
    """Best-effort bootout so a following ``bootstrap`` can't hit EBUSY.

    ``launchctl bootstrap`` fails if the label is already loaded, so `install
    --force` (reconfigure) and `start`-while-running would error and silently
    leave the OLD daemon live. Unload first, ignoring "not loaded" failures.
    """
    with contextlib.suppress(Exception):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            manager_argv("darwin", "stop", plan, uid=uid)[0],
            check=False,
            capture_output=True,
        )


def _paired(settings: Settings) -> bool:
    creds = store.load(settings.credentials_path)
    return bool(creds and creds.get("node_token"))


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None


def cmd_service_install(settings: Settings, *, force: bool) -> int:
    plan = _plan(settings)
    if plan is None:
        print(
            f"service install isn't supported on this platform ({_system() or 'unknown'}). "
            "See docs/deploy/ for manual templates.",
            file=sys.stderr,
        )
        return 2
    # The daemon can't pair unattended; refuse rather than register a service
    # that will just exit 1 in a loop.
    if not _paired(settings):
        print("Not paired. Run `coding-bridge pair` first.", file=sys.stderr)
        return 1
    if plan.path.exists() and not force:
        print(f"Refusing to overwrite existing {plan.path} (pass --force).", file=sys.stderr)
        return 1
    try:
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        plan.path.write_text(plan.content, encoding="utf-8")
        if os.name == "posix":
            with contextlib.suppress(OSError):
                plan.path.chmod(0o600)  # unit file may reference private paths
    except OSError as exc:
        print(f"could not write {plan.path}: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    print(f"Wrote {plan.kind} unit -> {plan.path}")
    if _system() == "darwin":
        _launchd_preclean(plan, _uid())  # avoid bootstrap EBUSY on --force reinstall
    rc = _run(manager_argv(_system(), "install", plan, uid=_uid()))
    if rc == 0:
        print(f"Service '{plan.label}' installed and started.")
        for note in plan.notes:
            print(f"note: {note}")
    return rc


def cmd_service_action(settings: Settings, action: str) -> int:
    """start / stop / status / uninstall."""
    plan = _plan(settings)
    if plan is None:
        print(
            f"service {action} isn't supported on this platform ({_system() or 'unknown'}).",
            file=sys.stderr,
        )
        return 2
    if action in ("start", "status") and not plan.path.exists():
        print(
            f"No service unit at {plan.path}. Run `coding-bridge service install` first.",
            file=sys.stderr,
        )
        return 1
    if action == "start" and _system() == "darwin":
        _launchd_preclean(plan, _uid())  # bootstrap errors if already loaded
    if action == "start" and _system() == "darwin":
        _launchd_preclean(plan, _uid())  # bootstrap errors if already loaded
    rc = _run(manager_argv(_system(), action, plan, uid=_uid()))
    if action == "uninstall":
        # Remove the unit file after deregistering, regardless of the manager's
        # exit code (the task may already be gone).
        with contextlib.suppress(OSError):
            plan.path.unlink()
        if _system() == "linux":
            _run([["systemctl", "--user", "daemon-reload"]])
        print(f"Service '{plan.label}' removed.")
        return 0
    if action == "stop":
        # "already stopped / not loaded" is the desired end state, not an error.
        print(f"Service '{plan.label}' stopped.")
        return 0
    return rc


# ---------- argparse wiring ---------------------------------------------------


def register_subparsers(
    service_parser: argparse.ArgumentParser, common: argparse.ArgumentParser
) -> None:
    """Attach install/start/stop/status/uninstall under ``service``."""
    sub = service_parser.add_subparsers(dest="service_command", required=True)

    p_install = sub.add_parser(
        "install",
        help="Register the daemon as a user service and start it at login/boot",
        parents=[common],
    )
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing unit.")
    p_install.set_defaults(func=_dispatch_install)

    for name, helptext in (
        ("start", "Start the installed service now"),
        ("stop", "Stop the running service"),
        ("status", "Show the service manager's status for the daemon"),
        ("uninstall", "Stop, deregister, and remove the service unit"),
    ):
        p = sub.add_parser(name, help=helptext, parents=[common])
        p.set_defaults(func=_dispatch_action, service_action=name)


def _dispatch_install(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(cmd_service_install(_build_settings(args), force=args.force))


def _dispatch_action(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(cmd_service_action(_build_settings(args), args.service_action))


__all__ = [
    "cmd_service_install",
    "cmd_service_action",
    "register_subparsers",
]
