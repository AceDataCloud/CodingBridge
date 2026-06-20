"""Logging setup for the node daemon.

Two sinks, no secrets (this runs on an end user's machine):

* a rotating file at ``<config_dir>/logs/agent.log`` for local post-mortem, and
* stderr for live ``coding-bridge run`` output.

A third, *optional* sink — :class:`BridgeLogForwarder` — streams structured
records up to the relay as ``node.log`` envelopes. The relay (which *does* hold
CLS credentials) ships them to Tencent CLS, giving us one correlated event
stream across browser → relay → node without ever putting a cloud secret on the
user's box.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

ROOT_LOGGER = "coding-bridge"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(level: str = "INFO", log_dir: Path | None = None) -> Path | None:
    """Configure the package logger with stderr + rotating-file handlers.

    Returns the log file path (or ``None`` if a file sink could not be opened).
    Idempotent: safe to call more than once.
    """
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(stream)

    log_path: Path | None = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "agent.log"
            already = any(
                isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers
            )
            if not already:
                file_handler = logging.handlers.RotatingFileHandler(
                    log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
                )
                file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
                logger.addHandler(file_handler)
        except OSError:
            # A read-only home or odd sandbox shouldn't stop the daemon.
            log_path = None
    return log_path


def _coerce_level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


class BridgeLogForwarder(logging.Handler):
    """Forward log records to the relay as ``node.log`` envelopes.

    Attached only once a connection is live. ``send`` is the connection's
    coroutine that wraps a payload in a ``node.log`` envelope; ``schedule`` runs
    that coroutine on the daemon's event loop from any thread. Failures are
    swallowed — logging must never break the relay.
    """

    def __init__(
        self,
        send: Callable[[dict], Awaitable[None]],
        schedule: Callable[[Awaitable[None]], None],
        *,
        level: int = logging.INFO,
    ) -> None:
        super().__init__(level=level)
        self._send = send
        self._schedule = schedule

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "level": record.levelname.lower(),
                "logger": record.name,
                "msg": record.getMessage(),
                "trace_id": getattr(record, "trace_id", None),
                "session_id": getattr(record, "session_id", None),
            }
            self._schedule(self._send(payload))
        except Exception:  # noqa: BLE001 - never let logging raise
            self.handleError(record)
