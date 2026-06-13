import httpx
import pytest
from sluice_worker.broker_client import BrokerClient, TokenExpired


def _client(handler):
    return BrokerClient(base_url="http://gw", token="T", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_jwt_on_control_endpoints_not_on_object_io():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/internal/v1/lease":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, content=b"ok")

    bc = _client(handler)
    await bc.lease(4)  # control -> authed (to the gateway)
    await bc.get("https://s3.example/obj?sig=x")  # object I/O -> bare, no JWT to storage
    await bc.put("https://s3.example/put?sig=y", b"data")
    await bc.aclose()
    by_url = dict(seen)
    assert by_url["http://gw/internal/v1/lease"] == "Bearer T"
    assert by_url["https://s3.example/obj?sig=x"] is None
    assert by_url["https://s3.example/put?sig=y"] is None


@pytest.mark.asyncio
async def test_lease_401_raises_token_expired():
    def handler(request):
        return httpx.Response(401, text="expired")

    bc = _client(handler)
    with pytest.raises(TokenExpired):
        await bc.lease(4)
    await bc.aclose()
