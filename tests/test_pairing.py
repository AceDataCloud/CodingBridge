import httpx
import pytest
import respx

from coding_bridge_agent.config import Settings
from coding_bridge_agent.pairing import PairingError, poll_for_token, start_pairing


@respx.mock
async def test_start_and_poll():
    settings = Settings(bridge_url="https://bridge.test")
    respx.post("https://bridge.test/pair/start").mock(
        return_value=httpx.Response(200, json={"pair_code": "ABCD1234", "expires_in": 600})
    )
    code, expires_in = await start_pairing(settings)
    assert code == "ABCD1234"
    assert expires_in == 600

    respx.post("https://bridge.test/pair/poll").mock(
        side_effect=[
            httpx.Response(200, json={"status": "pending"}),
            httpx.Response(200, json={"status": "ready", "node_token": "node_xyz"}),
        ]
    )
    token = await poll_for_token(settings, "ABCD1234", interval=0.01)
    assert token == "node_xyz"


@respx.mock
async def test_poll_expired_raises():
    settings = Settings(bridge_url="https://bridge.test")
    respx.post("https://bridge.test/pair/poll").mock(
        return_value=httpx.Response(200, json={"status": "expired"})
    )
    with pytest.raises(PairingError):
        await poll_for_token(settings, "X", interval=0.01)


@respx.mock
async def test_start_failure_raises():
    settings = Settings(bridge_url="https://bridge.test")
    respx.post("https://bridge.test/pair/start").mock(return_value=httpx.Response(500))
    with pytest.raises(PairingError):
        await start_pairing(settings)
