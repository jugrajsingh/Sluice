import httpx
import pytest
from sluice_worker.broker_client import BrokerClient, TokenExpired


def _client(handler):
    return BrokerClient(base_url="http://gw", token="T", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_jwt_only_sent_to_gateway_not_object_storage():
    seen = {}

    def handler(request):
        seen[str(request.url)] = request.headers.get("authorization")
        return httpx.Response(200, content=b"ok")

    bc = _client(handler)
    await bc.get("/internal/v1/blob/seg/requests/r1")  # proxy -> gateway, authed
    await bc.get("https://s3.example/obj?sig=x")  # cloud -> bare, no auth
    await bc.put("https://s3.example/put?sig=y", b"data")
    await bc.aclose()
    assert seen["http://gw/internal/v1/blob/seg/requests/r1"] == "Bearer T"
    assert seen["https://s3.example/obj?sig=x"] is None
    assert seen["https://s3.example/put?sig=y"] is None


@pytest.mark.asyncio
async def test_lease_401_raises_token_expired():
    def handler(request):
        return httpx.Response(401, text="expired")

    bc = _client(handler)
    with pytest.raises(TokenExpired):
        await bc.lease(4)
    await bc.aclose()
