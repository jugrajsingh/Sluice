import pytest
from sluice_core.inference_objects import ObjectStoreInferenceObjects


class FakeStore:
    async def signed_url(self, key, *, method="GET", expires_s=900):
        return f"https://signed/{method}/{key}?exp={expires_s}"


@pytest.mark.asyncio
async def test_signed_get_request_uses_request_key():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    url = await io.signed_get_request("seg", "rid1", expires_s=600)
    assert url == "https://signed/GET/apps/seg/requests/rid1?exp=600"


@pytest.mark.asyncio
async def test_signed_put_result_uses_result_key():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    url = await io.signed_put_result("seg", "rid1", expires_s=600)
    assert url == "https://signed/PUT/apps/seg/results/rid1?exp=600"


def test_request_and_result_keys():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    assert io.request_key("seg", "rid1") == "apps/seg/requests/rid1"
    assert io.result_key("seg", "rid1") == "apps/seg/results/rid1"
