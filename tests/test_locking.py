"""The single-instance lock prevents two daemons from fighting over one token."""
import pytest

from coding_bridge.locking import AlreadyRunning, SingleInstance


def test_second_acquire_is_rejected(tmp_path):
    lock_path = tmp_path / "agent.lock"
    first = SingleInstance(lock_path)
    first.acquire()
    try:
        second = SingleInstance(lock_path)
        with pytest.raises(AlreadyRunning):
            second.acquire()
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path):
    lock_path = tmp_path / "agent.lock"
    a = SingleInstance(lock_path)
    a.acquire()
    a.release()
    # Once released, another instance can take it.
    b = SingleInstance(lock_path)
    b.acquire()
    b.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "agent.lock"
    with SingleInstance(lock_path), pytest.raises(AlreadyRunning):
        SingleInstance(lock_path).acquire()
    # After the with-block, the lock is free again.
    again = SingleInstance(lock_path)
    again.acquire()
    again.release()


def test_lock_file_records_pid(tmp_path):
    import os

    lock_path = tmp_path / "agent.lock"
    lock = SingleInstance(lock_path)
    lock.acquire()
    try:
        assert lock_path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()
