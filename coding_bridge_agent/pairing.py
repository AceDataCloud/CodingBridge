"""Device-flow pairing client.

The node calls ``POST /pair/start`` to obtain a short pair code, shows it to the
user, then polls ``POST /pair/poll`` until the user claims it from Nexior (which
calls ``/pair/claim`` with their Ace JWT). The poll then returns the node_token.
"""
from __future__ import annotations

import asyncio

import httpx

from .config import Settings


class PairingError(Exception):
    pass


async def start_pairing(settings: Settings) -> tuple[str, int]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            settings.pair_start_url, json={"node_name": settings.node_name}
        )
    if response.status_code != 200:
        raise PairingError(f"pair/start failed: HTTP {response.status_code}")
    data = response.json()
    return data["pair_code"], int(data.get("expires_in", 600))


async def poll_for_token(
    settings: Settings,
    pair_code: str,
    *,
    interval: float = 2.0,
    deadline: float | None = None,
) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            response = await client.post(settings.pair_poll_url, json={"pair_code": pair_code})
            if response.status_code == 200:
                data = response.json()
                status = data.get("status")
                if status == "ready" and data.get("node_token"):
                    return data["node_token"]
                if status in {"expired", "consumed"}:
                    raise PairingError(f"pairing {status}")
            elif response.status_code == 404:
                raise PairingError("pairing expired")
            if deadline is not None and asyncio.get_running_loop().time() > deadline:
                raise PairingError("pairing timed out")
            await asyncio.sleep(interval)
