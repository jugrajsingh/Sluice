import pytest
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.errors import KeyNotFound
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.interfaces import InferenceObjects


async def test_request_and_result_roundtrip(tmp_path):
    objs = ObjectStoreInferenceObjects(store=LocalObjectStore(root=str(tmp_path)))
    assert isinstance(objs, InferenceObjects)
    await objs.put_request("topwear", "h1", b"img-bytes")
    assert await objs.get_request("topwear", "h1") == b"img-bytes"
    assert await objs.result_exists("topwear", "h1") is False
    await objs.put_result("topwear", "h1", b"masks")
    assert await objs.result_exists("topwear", "h1") is True
    assert await objs.get_result("topwear", "h1") == b"masks"


async def test_paths_follow_prefix_template(tmp_path):
    store = LocalObjectStore(root=str(tmp_path))
    objs = ObjectStoreInferenceObjects(store=store, prefix_template="apps/{app}")
    await objs.put_request("m", "r1", b"x")
    assert await store.exists("apps/m/requests/r1")


async def test_get_result_missing_raises(tmp_path):
    objs = ObjectStoreInferenceObjects(store=LocalObjectStore(root=str(tmp_path)))
    with pytest.raises(KeyNotFound):
        await objs.get_result("m", "nope")
