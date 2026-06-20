import pytest
from sluice_core.inference_objects import ObjectStoreInferenceObjects


class FakeStore:
    async def signed_url(self, key, *, method="GET", expires_s=900):
        return f"https://signed/{method}/{key}?exp={expires_s}"


@pytest.mark.asyncio
async def test_signed_get_request_uses_request_key():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    url = await io.signed_get_request("seg", "rid1", expires_s=600)
    assert url == "https://signed/GET/AppData/seg/requests/rid1?exp=600"


@pytest.mark.asyncio
async def test_signed_put_result_uses_result_key():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    url = await io.signed_put_result("seg", "rid1", expires_s=600)
    assert url == "https://signed/PUT/AppData/seg/results/rid1.gz?exp=600"  # results always gzipped


def test_request_and_result_keys():
    io = ObjectStoreInferenceObjects(store=FakeStore())
    assert io.request_key("seg", "rid1") == "AppData/seg/requests/rid1"
    assert io.result_key("seg", "rid1") == "AppData/seg/results/rid1.gz"  # .gz marks the always-gzipped result
