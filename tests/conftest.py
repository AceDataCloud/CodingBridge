import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _no_codex_stream_delay(monkeypatch):
    """Keep the codex typewriter instant so tests stay fast and deterministic."""
    from coding_bridge.providers import codex

    monkeypatch.setattr(codex, "STREAM_DELAY", 0, raising=False)
