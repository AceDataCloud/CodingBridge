"""Command-line interface: ``pair``, ``run``, ``up``, ``status``, ``logout``."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import capabilities, logs, store
from .config import Settings
from .connection import BridgeConnection
from .locking import AlreadyRunning, SingleInstance
from .pairing import PairingError, poll_for_token, start_pairing


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "bridge_url", None):
        settings.bridge_url = args.bridge_url
    if getattr(args, "name", None):
        settings.node_name = args.name
    if getattr(args, "config_dir", None):
        settings.config_dir = Path(args.config_dir).expanduser()
        settings.log_dir = settings.config_dir / "logs"
    if getattr(args, "model", None):
        settings.default_model = args.model
    if getattr(args, "claude_path", None):
        settings.claude_path = args.claude_path
    if getattr(args, "codex_path", None):
        settings.codex_path = args.codex_path
    if getattr(args, "cwd", None):
        settings.default_cwd = args.cwd
    if getattr(args, "permission_timeout", None) is not None:
        settings.permission_timeout = args.permission_timeout
    if getattr(args, "log_dir", None):
        settings.log_dir = Path(args.log_dir).expanduser()
    if getattr(args, "log_level", None):
        settings.log_level = args.log_level
    if getattr(args, "verbose", False):
        settings.log_level = "DEBUG"
    return settings


def _print_pairing(settings: Settings, pair_code: str) -> None:
    claim_base = settings.claim_url_template.split("?", 1)[0]
    claim_url = settings.claim_url_template.format(code=pair_code)
    print()
    print("  Pair this machine with your Ace account:")
    print(f"    1. Open {claim_base}")
    print(f"    2. Enter pair code:  {pair_code}")
    print(f"    or open directly:    {claim_url}")
    _print_qr(claim_url)
    print()


def _print_qr(data: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def _do_pair(settings: Settings) -> str:
    pair_code, expires_in = await start_pairing(settings)
    _print_pairing(settings, pair_code)
    print(f"  Waiting for confirmation (expires in {expires_in}s)...")
    deadline = asyncio.get_running_loop().time() + expires_in
    token = await poll_for_token(settings, pair_code, deadline=deadline)
    store.save(
        settings.credentials_path,
        {"node_token": token, "node_name": settings.node_name, "bridge_url": settings.bridge_url},
    )
    print(f"  Paired. Credentials saved to {settings.credentials_path}")
    return token


async def _run_connection(settings: Settings, token: str) -> None:
    # Refuse to start a second daemon for this device: two agents sharing one
    # node token fight over the relay slot and tear down every session.
    lock = SingleInstance(settings.config_dir / "agent.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        print(
            "Another coding-bridge is already running for this device.\n"
            "Stop it before starting a new one — two instances fight over the\n"
            "connection and break every session. If it autostarts (a service or\n"
            "scheduled task), do not also run it manually.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    # A daemon launched outside the user's login shell (no nvm/volta/.local on
    # PATH) can't see `claude`/`codex` even when installed; surface the resolved
    # dirs onto PATH so the SDK and `codex exec` actually find them.
    added = capabilities.ensure_clis_on_path(settings)
    if added:
        logging.getLogger(logs.ROOT_LOGGER).info("added to PATH for CLI discovery: %s", added)
    connection = BridgeConnection(settings, token)
    print(f"  Coding Bridge agent running. Node: {settings.node_name}. Press Ctrl-C to stop.")
    try:
        await connection.run()
    finally:
        await connection.aclose()
        lock.release()


def cmd_pair(args: argparse.Namespace) -> None:
    settings = _build_settings(args)
    try:
        asyncio.run(_do_pair(settings))
    except PairingError as exc:
        print(f"Pairing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_run(args: argparse.Namespace) -> None:
    settings = _build_settings(args)
    creds = store.load(settings.credentials_path)
    if not creds or not creds.get("node_token"):
        print("Not paired. Run `coding-bridge pair` first.", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(_run_connection(settings, creds["node_token"]))


def cmd_up(args: argparse.Namespace) -> None:
    settings = _build_settings(args)
    creds = store.load(settings.credentials_path)
    token = creds.get("node_token") if creds else None

    async def _go() -> None:
        nonlocal token
        if not token:
            token = await _do_pair(settings)
        await _run_connection(settings, token)

    try:
        asyncio.run(_go())
    except PairingError as exc:
        print(f"Pairing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_logout(args: argparse.Namespace) -> None:
    settings = _build_settings(args)
    removed = store.clear(settings.credentials_path)
    print("Credentials removed." if removed else "No credentials found.")


def cmd_status(args: argparse.Namespace) -> None:
    settings = _build_settings(args)
    creds = store.load(settings.credentials_path)
    paired = bool(creds and creds.get("node_token"))
    print(f"Bridge URL : {settings.bridge_url}")
    print(f"Node name  : {settings.node_name}")
    print(f"Config dir : {settings.config_dir}")
    print(f"Paired     : {'yes' if paired else 'no'}")


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", help="Default Claude model for new sessions")
    parser.add_argument("--cwd", help="Default working directory for new sessions")
    parser.add_argument(
        "--claude-path",
        dest="claude_path",
        help="Path to the claude CLI (when PATH can't find it, e.g. nvm installs)",
    )
    parser.add_argument(
        "--codex-path",
        dest="codex_path",
        help="Path to the codex CLI (when PATH can't find it)",
    )
    parser.add_argument(
        "--permission-timeout",
        type=float,
        dest="permission_timeout",
        help="Seconds to wait for a permission decision (0 = forever)",
    )


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bridge-url", default=argparse.SUPPRESS, help="coding-bridge base URL")
    common.add_argument("--name", default=argparse.SUPPRESS, help="Display name for this node")
    common.add_argument(
        "--config-dir", default=argparse.SUPPRESS, help="Where credentials are stored"
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable debug logging",
    )
    common.add_argument(
        "--log-level",
        dest="log_level",
        default=argparse.SUPPRESS,
        help="Log level (DEBUG, INFO, WARNING, ERROR)",
    )
    common.add_argument(
        "--log-dir",
        dest="log_dir",
        default=argparse.SUPPRESS,
        help="Directory for rotating log files",
    )

    parser = argparse.ArgumentParser(
        prog="coding-bridge",
        description="Run Claude Code on this machine, driven from the AceDataCloud web app.",
        parents=[common],
    )
    parser.set_defaults(func=cmd_up)

    sub = parser.add_subparsers(dest="command")

    p_up = sub.add_parser("up", help="Pair if needed, then run (default)", parents=[common])
    _add_run_args(p_up)
    p_up.set_defaults(func=cmd_up)

    sub.add_parser("pair", help="Pair this machine and exit", parents=[common]).set_defaults(
        func=cmd_pair
    )

    p_run = sub.add_parser("run", help="Run using stored credentials", parents=[common])
    _add_run_args(p_run)
    p_run.set_defaults(func=cmd_run)

    sub.add_parser(
        "status", help="Show configuration and pairing state", parents=[common]
    ).set_defaults(func=cmd_status)
    sub.add_parser("logout", help="Remove stored credentials", parents=[common]).set_defaults(
        func=cmd_logout
    )

    args = parser.parse_args(argv)
    settings = _build_settings(args)
    log_path = logs.setup(settings.log_level, settings.log_dir)
    if log_path is not None:
        logging.getLogger(logs.ROOT_LOGGER).debug("logging to %s", log_path)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
