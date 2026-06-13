"""Single-instance lock so only one daemon runs per config dir.

Two ``coding-bridge-agent up`` processes sharing one node token both connect to
the relay with the same identity; the relay keeps only the newest, so they
supersede each other in a tight reconnect loop that tears down every in-flight
turn. This is easy to hit by accident — e.g. an autostart task plus a manual run
in a terminal. The lock makes the second process fail fast with a clear message
instead of silently fighting the first.

The lock is an OS advisory file lock (``fcntl`` on POSIX, ``msvcrt`` on Windows)
held for the life of the process, so a crash releases it automatically — the
autostart task can restart cleanly without a stale lock.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class AlreadyRunning(Exception):
    """Another agent already holds the lock for this config dir."""


class SingleInstance:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        """Take the lock, or raise :class:`AlreadyRunning` if another holds it."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Held open for the life of the process (released by the OS on exit), so
        # this is deliberately not a `with` block.
        handle = open(self.lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            self._lock(handle)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(str(self.lock_path)) from exc
        # Record our pid for humans inspecting the lock file.
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        self._unlock(self._handle)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[str]) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Releasing is best-effort; the OS frees the lock on close/exit anyway.
            pass
