"""Channels config schema — parses ``~/.ace-bridge/channels.toml``.

Split from ``coding_bridge.config`` on purpose: channels are optional and evolve
independently of the core node settings. Users who never touch channels never
load this module.

Schema (fail-fast; unknown keys raise):

.. code-block:: toml

    [channels]
    # (reserved for future globals)

    [[channels.wechat]]
    instance_id = "beijing-cvm"        # required, unique per file
    base_url = "http://82.156.126.14:8000"  # required, http:// or https://
    token_env = "WECHAT_TOKEN_BEIJING"   # required unless token_file is set
    token_file = "/run/secrets/beijing"  # optional alternative to token_env
    enabled = false                      # default false — explicit opt-in
    default_provider = "claude"          # optional override for this instance

Design notes:

* The **token itself never lives in the TOML file** — the file references an
  env var name (`token_env`) or a secrets-file path (`token_file`). This
  matches the deployment templates in P8 (systemd EnvironmentFile,
  launchd, Windows service credentials store).
* `enabled = false` is the default so accidentally shipping a `channels.toml`
  never puts an unsupervised bot online.
* Unknown keys raise — a typo like `intance_id` or `allowlist_typo` should
  fail loudly at boot, not silently degrade to unsafe defaults.
* A hard 1 MB cap on the TOML file guards against accidentally pointing at
  ``/dev/urandom`` or a runaway log.
* ``base_url`` may not contain embedded userinfo (``http://user:pass@host``);
  credentials go through ``token_env``/``token_file`` only.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .policy import ChannelPolicy

try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore[import-not-found]

_WECHAT_KEYS = frozenset(
    {
        "instance_id",
        "base_url",
        "token_env",
        "token_file",
        "enabled",
        "default_provider",
        "trigger_prefix",
        "allowed_senders",
        "rate_limit_per_min",
        "dedup_window_seconds",
    }
)
_CHANNELS_KEYS = frozenset({"wechat"})

# Cap the TOML file size so a misconfigured `channels.toml` pointing at
# `/dev/urandom` or similar can't OOM the daemon at startup.
_MAX_TOML_BYTES = 1_000_000

# Cap the token file so `token_file = "/dev/urandom"` also can't OOM us.
# Real API tokens are ~64–512 bytes; 64 KB is generous.
_MAX_TOKEN_BYTES = 64 * 1024


class ConfigError(ValueError):
    """Raised for any schema violation — never caught silently."""


@dataclass(frozen=True)
class WeChatInstanceConfig:
    """One `[[channels.wechat]]` block."""

    instance_id: str
    base_url: str
    token_env: str | None = None
    token_file: str | None = None
    enabled: bool = False
    default_provider: str | None = None
    #: Message text must start with this to be forwarded. Empty string disables
    #: the check. See ``coding_bridge.channels.policy.ChannelPolicy``.
    trigger_prefix: str = "/ask "
    #: Sender allowlist. Empty tuple = allow all (still gated by the WeChat
    #: gateway token). Stored as tuple because ``WeChatInstanceConfig`` is frozen.
    allowed_senders: tuple[str, ...] = ()
    #: Sliding-window rate limit per sender_id over the last 60 s.
    rate_limit_per_min: int = 6
    #: Dedup window for repeat upstream ``msg_id`` (upstream retries). ``0``
    #: disables dedup.
    dedup_window_seconds: float = 300.0

    def to_policy(self) -> ChannelPolicy:
        """Build the runtime ``ChannelPolicy`` this instance describes."""
        # Local import to avoid a circular module cycle
        # (policy → base → nothing config-specific; config → policy would loop
        # only at type-annotation time, but keep the runtime import lazy so
        # ``coding_bridge.channels.config`` alone still loads without importing
        # the whole channels stack).
        from .policy import ChannelPolicy

        return ChannelPolicy(
            trigger_prefix=self.trigger_prefix,
            allowed_senders=self.allowed_senders,
            rate_limit_per_min=self.rate_limit_per_min,
            dedup_window_seconds=self.dedup_window_seconds,
        )

    def resolve_token(self, environ: dict[str, str] | None = None) -> str:
        """Load the token from env var or secrets file. Never logs it.

        Raises ``ConfigError`` with a **redacted** message if the token can't
        be resolved — the message references the env-var *name* or file path,
        never the value.
        """
        env = environ if environ is not None else dict(os.environ)
        if self.token_env:
            token = env.get(self.token_env)
            if not token:
                raise ConfigError(
                    f"wechat instance {self.instance_id!r}: env var "
                    f"{self.token_env!r} is unset or empty"
                )
            return token
        if self.token_file:
            path = Path(self.token_file).expanduser()
            try:
                # Reject anything that isn't a regular file up front so a
                # symlink pointing at a directory / device raises a clean
                # ConfigError instead of a raw OSError / UnicodeDecodeError.
                if not path.is_file():
                    raise ConfigError(
                        f"wechat instance {self.instance_id!r}: token_file "
                        f"{str(path)!r} is not a regular file"
                    )
                size = path.stat().st_size
                if size > _MAX_TOKEN_BYTES:
                    raise ConfigError(
                        f"wechat instance {self.instance_id!r}: token_file "
                        f"{str(path)!r} exceeds {_MAX_TOKEN_BYTES}-byte limit"
                    )
                raw = path.read_bytes()
            except OSError as exc:
                raise ConfigError(
                    f"wechat instance {self.instance_id!r}: cannot read "
                    f"token_file {str(path)!r}: {exc.__class__.__name__}"
                ) from None
            try:
                token = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                raise ConfigError(
                    f"wechat instance {self.instance_id!r}: token_file "
                    f"{str(path)!r} is not valid UTF-8"
                ) from None
            if not token:
                raise ConfigError(
                    f"wechat instance {self.instance_id!r}: token_file "
                    f"{str(path)!r} is empty"
                )
            return token
        raise ConfigError(
            f"wechat instance {self.instance_id!r}: either token_env or "
            "token_file must be set"
        )


@dataclass(frozen=True)
class ChannelsConfig:
    """Root of the channels config file."""

    wechat: tuple[WeChatInstanceConfig, ...] = field(default_factory=tuple)

    @property
    def enabled_wechat(self) -> tuple[WeChatInstanceConfig, ...]:
        return tuple(inst for inst in self.wechat if inst.enabled)


def _require(kind: str, block: dict[str, Any], key: str) -> Any:
    value = block.get(key)
    if value is None or value == "":
        raise ConfigError(f"{kind}: required field {key!r} is missing or empty")
    return value


def _parse_wechat(block: dict[str, Any], index: int) -> WeChatInstanceConfig:
    unknown = set(block.keys()) - _WECHAT_KEYS
    if unknown:
        raise ConfigError(
            f"[[channels.wechat]] #{index}: unknown key(s) {sorted(unknown)!r}"
        )
    instance_id = _require(f"[[channels.wechat]] #{index}", block, "instance_id")
    base_url = _require(f"[[channels.wechat]] #{index}", block, "base_url")
    if not isinstance(instance_id, str):
        raise ConfigError(f"[[channels.wechat]] #{index}: instance_id must be a string")
    if not isinstance(base_url, str):
        raise ConfigError(f"[[channels.wechat]] #{index}: base_url must be a string")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: base_url must start with "
            "http:// or https://"
        )
    # Reject `http://user:pass@host` so tokens never travel via base_url.
    # `urlparse` folds userinfo into `netloc`; a bare `@` in the path segment
    # after the host doesn't count because it's not in netloc.
    if "@" in urlparse(base_url).netloc:
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: base_url must not contain "
            "embedded credentials (user:pass@host); use token_env/token_file"
        )
    token_env = block.get("token_env")
    token_file = block.get("token_file")
    if token_env is not None and not isinstance(token_env, str):
        raise ConfigError(f"[[channels.wechat]] {instance_id!r}: token_env must be a string")
    if token_file is not None and not isinstance(token_file, str):
        raise ConfigError(f"[[channels.wechat]] {instance_id!r}: token_file must be a string")
    if token_env and token_file:
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: set token_env OR token_file, not both"
        )
    enabled = block.get("enabled", False)
    # `isinstance(True, int) is True` in Python, so guard against the reverse
    # (`enabled = 1` in TOML → int → we want a hard error, not a silent truthy).
    if not isinstance(enabled, bool):
        raise ConfigError(f"[[channels.wechat]] {instance_id!r}: enabled must be a bool")
    default_provider = block.get("default_provider")
    if default_provider is not None and not isinstance(default_provider, str):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: default_provider must be a string"
        )

    trigger_prefix = block.get("trigger_prefix", "/ask ")
    if not isinstance(trigger_prefix, str):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: trigger_prefix must be a string"
        )

    allowed_senders_raw = block.get("allowed_senders", [])
    if not isinstance(allowed_senders_raw, list) or not all(
        isinstance(x, str) and x for x in allowed_senders_raw
    ):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: allowed_senders must be a "
            "list of non-empty strings"
        )

    rate_limit = block.get("rate_limit_per_min", 6)
    # Bool is a subclass of int in Python — reject explicit bools to catch
    # `rate_limit_per_min = true` typos.
    if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or rate_limit < 0:
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: rate_limit_per_min must be "
            "a non-negative int"
        )

    dedup_window = block.get("dedup_window_seconds", 300.0)
    if isinstance(dedup_window, bool) or not isinstance(dedup_window, (int, float)):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: dedup_window_seconds must "
            "be a number"
        )
    # NaN < 0 is False so a plain range check misses it — reject explicitly
    # so ``_prune_dedup`` never sees a non-comparable value.
    if math.isnan(dedup_window) or math.isinf(dedup_window):
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: dedup_window_seconds must "
            "be finite"
        )
    if dedup_window < 0:
        raise ConfigError(
            f"[[channels.wechat]] {instance_id!r}: dedup_window_seconds must "
            "be >= 0"
        )

    return WeChatInstanceConfig(
        instance_id=instance_id,
        base_url=base_url.rstrip("/"),
        token_env=token_env,
        token_file=token_file,
        enabled=enabled,
        default_provider=default_provider,
        trigger_prefix=trigger_prefix,
        allowed_senders=tuple(allowed_senders_raw),
        rate_limit_per_min=rate_limit,
        dedup_window_seconds=float(dedup_window),
    )


def parse_channels_config(data: dict[str, Any]) -> ChannelsConfig:
    """Parse an already-decoded TOML mapping. Public for test use."""
    channels = data.get("channels")
    if channels is None:
        return ChannelsConfig()
    if not isinstance(channels, dict):
        raise ConfigError("[channels]: must be a table")
    unknown = set(channels.keys()) - _CHANNELS_KEYS
    if unknown:
        raise ConfigError(f"[channels]: unknown key(s) {sorted(unknown)!r}")

    wechat_blocks = channels.get("wechat", [])
    if not isinstance(wechat_blocks, list):
        raise ConfigError("[[channels.wechat]]: must be an array of tables")

    parsed: list[WeChatInstanceConfig] = []
    seen_ids: set[str] = set()
    for i, block in enumerate(wechat_blocks):
        if not isinstance(block, dict):
            raise ConfigError(f"[[channels.wechat]] #{i}: must be a table")
        inst = _parse_wechat(block, i)
        if inst.instance_id in seen_ids:
            raise ConfigError(
                f"[[channels.wechat]]: duplicate instance_id {inst.instance_id!r}"
            )
        seen_ids.add(inst.instance_id)
        parsed.append(inst)
    return ChannelsConfig(wechat=tuple(parsed))


def load_channels_config(path: Path) -> ChannelsConfig:
    """Read + parse a TOML file. Missing file → empty config (no channels).

    Wraps every filesystem / decode error in ``ConfigError`` so the daemon
    can surface a clean startup message instead of an unhandled traceback.
    """
    if not path.exists():
        return ChannelsConfig()
    if not path.is_file():
        raise ConfigError(f"channels config {str(path)!r} is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigError(
            f"channels config {str(path)!r}: cannot stat: {exc.__class__.__name__}"
        ) from None
    if size > _MAX_TOML_BYTES:
        raise ConfigError(
            f"channels config {str(path)!r} exceeds {_MAX_TOML_BYTES}-byte limit "
            f"({size} bytes)"
        )
    try:
        with path.open("rb") as f:
            data = _toml.load(f)
    except _toml.TOMLDecodeError as exc:
        raise ConfigError(
            f"channels config {str(path)!r} is not valid TOML: {exc}"
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"channels config {str(path)!r}: cannot read: {exc.__class__.__name__}"
        ) from None
    return parse_channels_config(data)


__all__ = [
    "ChannelsConfig",
    "ConfigError",
    "WeChatInstanceConfig",
    "load_channels_config",
    "parse_channels_config",
]
