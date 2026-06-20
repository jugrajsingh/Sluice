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


@pytest.mark.asyncio
async def test_should_return_messages_with_batch_attributes_when_batch_leasing():
    """batch_lease maps broker JSON items to Message objects whose attributes carry
    job_id/file/body_url and whose ack_token is the lease_id — exactly what the
    adapter's batch lane reads."""

    def handler(request):
        assert request.url.path == "/internal/v1/batch/lease"
        assert request.headers.get("authorization") == "Bearer T"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "lease_id": "1-0",
                        "job_id": "J1",
                        "file": "a.jsonl",
                        "body_url": "https://s3/get/a",
                    }
                ]
            },
        )

    bc = _client(handler)
    msgs = await bc.batch_lease(1)
    await bc.aclose()
    assert len(msgs) == 1
    m = msgs[0]
    assert m.ack_token == "1-0"
    assert m.attributes == {"job_id": "J1", "file": "a.jsonl", "body_url": "https://s3/get/a"}


@pytest.mark.asyncio
async def test_should_post_to_batch_control_paths_when_acking_extending_nacking():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    bc = _client(handler)
    await bc.batch_ack("1-0")
    await bc.batch_extend(["1-0", "2-0"])
    await bc.batch_nack("1-0")
    await bc.aclose()
    paths = [p for p, _ in seen]
    assert paths == ["/internal/v1/batch/ack", "/internal/v1/batch/extend", "/internal/v1/batch/nack"]
    assert all(auth == "Bearer T" for _, auth in seen)
