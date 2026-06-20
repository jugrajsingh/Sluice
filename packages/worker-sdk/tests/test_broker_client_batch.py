import httpx
import pytest
from sluice_worker.broker_client import BrokerClient


def _handler(routes):
    async def h(request: httpx.Request) -> httpx.Response:
        return routes[(request.method, request.url.path)](request)

    return httpx.MockTransport(h)


@pytest.mark.asyncio
async def test_should_get_signed_url_when_batch_output_url():
    t = _handler(
        {("POST", "/internal/v1/batch/output-url"): lambda r: httpx.Response(200, json={"url": "https://signed/put"})}
    )
    bc = BrokerClient(base_url="http://gw", token="t", transport=t)  # gitleaks:allow (test token)
    assert await bc.batch_output_url("job1", "part-0.jsonl", 50) == "https://signed/put"
    await bc.aclose()


@pytest.mark.asyncio
async def test_should_return_none_when_status_absent():
    t = _handler(
        {("GET", "/internal/v1/batch/status"): lambda r: httpx.Response(200, json={"found": False, "status": None})}
    )
    bc = BrokerClient(base_url="http://gw", token="t", transport=t)  # gitleaks:allow (test token)
    assert await bc.batch_status_get("job1", "part-0.jsonl") is None
    await bc.aclose()


@pytest.mark.asyncio
async def test_should_pop_command_when_vm_command():
    t = _handler({("GET", "/internal/v1/vm/command"): lambda r: httpx.Response(200, json={"command": "shutdown"})})
    bc = BrokerClient(base_url="http://gw", token="t", transport=t)  # gitleaks:allow (test token)
    assert await bc.vm_command("vm1") == "shutdown"
    await bc.aclose()


@pytest.mark.asyncio
async def test_should_send_file_body_with_known_length_when_put_file(tmp_path):
    seen = {}

    def put(r):
        seen["body"] = r.content
        return httpx.Response(200)

    t = _handler({("PUT", "/put"): put})
    bc = BrokerClient(base_url="http://gw", token="t", transport=t)  # gitleaks:allow (test token)
    p = tmp_path / "f.jsonl"
    p.write_bytes(b"line1\nline2")
    await bc.put_file("http://obj/put", str(p))
    assert seen["body"] == b"line1\nline2"
    await bc.aclose()
