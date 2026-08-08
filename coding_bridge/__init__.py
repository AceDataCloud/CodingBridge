"""Coding Bridge Agent — node daemon for AceDataCloud Coding Bridge.

Runs on a developer's own machine, connects out to the coding-bridge relay, and
drives local Claude Code sessions on behalf of an authenticated browser. All
code execution stays local; the bridge only relays messages.
"""

from importlib.metadata import PackageNotFoundError, version

from .config import Settings
from .embed import SessionHost
from .protocol import Action, Event

try:
    __version__ = version("coding-bridge")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["Action", "Event", "SessionHost", "Settings", "__version__"]
