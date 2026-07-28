import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _no_codex_stream_delay(monkeypatch):
    """Keep the codex typewriter instant so tests stay fast and deterministic."""
    from coding_bridge.providers import codex

    monkeypatch.setattr(codex, "STREAM_DELAY", 0, raising=False)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Redirect ``~`` so sidecars never touch the developer's real ~/.ace-bridge.

    Deliberately not under ``tmp_path`` — tests that list ``tmp_path`` would see it.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
