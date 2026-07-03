"""Messaging channels: run a coding-agent turn per inbound external message.

Existing coding-bridge shape: browser ⇄ relay WSS ⇄ ``BridgeConnection`` node.
Channels shape: external messenger (WeChat / Telegram / …) ⇄ ``ChannelAdapter``
⇄ ``SessionDispatcher`` ⇄ existing ``Session`` + ``Provider``. The relay path is
untouched; channels are an additive second entry-point built on the same
``Provider`` layer.

See ``plans/coding-bridge-channels/01-production-plan.md`` in AceDataCloud/Index
for the P1..P11 roadmap this module implements.
"""

from .base import (
    ChannelAdapter,
    ChannelTarget,
    IncomingMessage,
    MessageHandler,
    SendResult,
)
from .config import (
    ChannelsConfig,
    ConfigError,
    WeChatInstanceConfig,
    load_channels_config,
    parse_channels_config,
)
from .dispatcher import SessionDispatcher
from .observability import TurnEvent, TurnOutcome, log_turn
from .policy import ChannelPolicy, PolicyGate

__all__ = [
    "ChannelAdapter",
    "ChannelPolicy",
    "ChannelTarget",
    "ChannelsConfig",
    "ConfigError",
    "IncomingMessage",
    "MessageHandler",
    "PolicyGate",
    "SendResult",
    "SessionDispatcher",
    "TurnEvent",
    "TurnOutcome",
    "WeChatInstanceConfig",
    "load_channels_config",
    "log_turn",
    "parse_channels_config",
]
