"""Telegram channel adapter (long-polling Bot API)."""

from __future__ import annotations

from .adapter import TelegramAdapter
from .client import TelegramClient, TelegramError

__all__ = ["TelegramAdapter", "TelegramClient", "TelegramError"]
