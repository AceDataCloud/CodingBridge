"""Runtime configuration for the node daemon."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRIDGE_URL = "https://coding-bridge.acedata.cloud"
DEFAULT_CONFIG_DIR = "~/.ace-bridge"
DEFAULT_CLAIM_URL = "https://studio.acedata.cloud/coding-bridge?code={code}"


def _default_node_name() -> str:
    try:
        return socket.gethostname() or "node"
    except OSError:
        return "node"


def _safe_default_cwd() -> str:
    """A sane default working directory.

    Normally the directory the daemon was launched from. But when it runs as a
    Windows service / from an OS launcher, ``os.getcwd()`` is ``C:\\Windows\\System32``
    — never a place to run code in. Fall back to the user's home in that case so a
    session that arrives without an explicit cwd (e.g. resumed-from-history when the
    transcript carried no cwd) doesn't silently land in System32.
    """
    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        return str(Path.home())
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if system_root:
        try:
            sys_dir = Path(system_root).resolve()
            if cwd == sys_dir or sys_dir in cwd.parents:
                return str(Path.home())
        except OSError:
            pass
    return str(cwd)


@dataclass
class Settings:
    """All tunables, sourced from env or CLI flags."""

    bridge_url: str = DEFAULT_BRIDGE_URL
    node_name: str = ""
    config_dir: Path = Path(DEFAULT_CONFIG_DIR)
    heartbeat_interval: float = 15.0
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0
    # Remote approval may arrive via a push notification minutes after the
    # prompt, so the window is generous. 0 → wait indefinitely for the user.
    permission_timeout: float = 1800.0
    turn_retry_limit: int = 1  # auto-retries when a provider subprocess crashes
    turn_retry_backoff: float = 0.5  # seconds between turn retries
    outbox_max: int = 5000  # max buffered node→browser events while disconnected
    default_cwd: str = ""
    default_model: str | None = None
    # Explicit paths to the provider CLIs, for nodes whose daemon PATH can't see
    # them (nvm/volta/.local installs). Empty → auto-resolve (PATH + known dirs).
    claude_path: str | None = None
    codex_path: str | None = None
    claim_url_template: str = DEFAULT_CLAIM_URL
    log_level: str = "INFO"
    log_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.node_name:
            self.node_name = _default_node_name()
        if not self.default_cwd:
            self.default_cwd = _safe_default_cwd()
        self.config_dir = Path(self.config_dir).expanduser()
        if self.log_dir is None:
            self.log_dir = self.config_dir / "logs"
        else:
            self.log_dir = Path(self.log_dir).expanduser()

    @property
    def _base(self) -> str:
        return self.bridge_url.rstrip("/")

    @property
    def ws_node_url(self) -> str:
        base = self._base
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/ws/node"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/ws/node"
        return base + "/ws/node"

    @property
    def pair_start_url(self) -> str:
        return f"{self._base}/pair/start"

    @property
    def pair_poll_url(self) -> str:
        return f"{self._base}/pair/poll"

    @property
    def credentials_path(self) -> Path:
        return self.config_dir / "credentials.json"

    @classmethod
    def from_env(cls) -> Settings:
        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw else default

        return cls(
            bridge_url=os.environ.get("CODING_BRIDGE_URL", DEFAULT_BRIDGE_URL),
            node_name=os.environ.get("CODING_BRIDGE_NODE_NAME", ""),
            config_dir=Path(os.environ.get("CODING_BRIDGE_CONFIG_DIR", DEFAULT_CONFIG_DIR)),
            heartbeat_interval=_f("CODING_BRIDGE_HEARTBEAT_INTERVAL", 15.0),
            permission_timeout=_f("CODING_BRIDGE_PERMISSION_TIMEOUT", 1800.0),
            turn_retry_limit=int(_f("CODING_BRIDGE_TURN_RETRY_LIMIT", 1)),
            turn_retry_backoff=_f("CODING_BRIDGE_TURN_RETRY_BACKOFF", 0.5),
            outbox_max=int(_f("CODING_BRIDGE_OUTBOX_MAX", 5000)),
            default_model=os.environ.get("CODING_BRIDGE_MODEL") or None,
            claude_path=os.environ.get("CODING_BRIDGE_CLAUDE_PATH") or None,
            codex_path=os.environ.get("CODING_BRIDGE_CODEX_PATH") or None,
            claim_url_template=os.environ.get("CODING_BRIDGE_CLAIM_URL", DEFAULT_CLAIM_URL),
            log_level=os.environ.get("CODING_BRIDGE_LOG_LEVEL", "INFO"),
            log_dir=(
                Path(os.environ["CODING_BRIDGE_LOG_DIR"])
                if os.environ.get("CODING_BRIDGE_LOG_DIR")
                else None
            ),
        )
