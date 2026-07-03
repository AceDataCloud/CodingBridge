"""WeChat channel adapter — personal-WeChat gateway integration.

The WeChat adapter is CodingBridge's first :class:`~coding_bridge.channels.ChannelAdapter`
concrete implementation. It talks to a personal-WeChat gateway (FastAPI, hosted
on a CVM) with two edges:

* ``wss://<host>/ws?token=<TOKEN>`` — the gateway broadcasts every WeChat message
  event as JSON ``{"event": "message.new", "data": {...}}``. The adapter
  filters ``direction=inbound`` structurally (echoes / outbound get skipped)
  and hands anything left to the dispatcher — policy (trigger prefix,
  allowlist, rate limit) lives one layer up in P7.
* ``POST /api/messages/send`` with ``Authorization: Bearer <TOKEN>`` — returns
  ``202 Accepted`` because the gateway queues a background UI task. P2 fires and
  forgets; P7 will poll ``/api/messages/tasks/{id}`` for delivery
  confirmation.

The adapter is import-safe without extras — it delegates to :mod:`websockets`
and :mod:`httpx`, both already required by CodingBridge core. The
``[wechat]`` extras marker exists only so users can express intent
(``pip install coding-bridge[wechat]``); no additional wheels are pulled.
"""

from .adapter import WeChatAdapter
from .client import WeChatClient

__all__ = ["WeChatAdapter", "WeChatClient"]
