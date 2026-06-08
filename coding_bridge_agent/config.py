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


@dataclass
class Settings:
    """All tunables, sourced from env or CLI flags."""

    bridge_url: str = DEFAULT_BRIDGE_URL
    node_name: str = ""
    config_dir: Path = Path(DEFAULT_CONFIG_DIR)
    heartbeat_interval: float = 15.0
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0
    permission_timeout: float = 300.0  # 0 → wait indefinitely for the user
    default_cwd: str = ""
    default_model: str | None = None
    claim_url_template: str = DEFAULT_CLAIM_URL

    def __post_init__(self) -> None:
        if not self.node_name:
            self.node_name = _default_node_name()
        if not self.default_cwd:
            self.default_cwd = os.getcwd()
        self.config_dir = Path(self.config_dir).expanduser()

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
            permission_timeout=_f("CODING_BRIDGE_PERMISSION_TIMEOUT", 300.0),
            default_model=os.environ.get("CODING_BRIDGE_MODEL") or None,
            claim_url_template=os.environ.get("CODING_BRIDGE_CLAIM_URL", DEFAULT_CLAIM_URL),
        )
