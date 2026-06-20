import pytest
from sluice_core.errors import KeyNotFound
from sluice_core.inference_objects import ObjectStoreInferenceObjects
from sluice_core.interfaces import InferenceObjects
from sluice_core.testing.fakes import FakeObjectStore


async def test_request_and_result_roundtrip():
    objs = ObjectStoreInferenceObjects(store=FakeObjectStore())
    assert isinstance(objs, InferenceObjects)
    await objs.put_request("topwear", "h1", b"img-bytes")
    assert await objs.get_request("topwear", "h1") == b"img-bytes"
    assert await objs.result_exists("topwear", "h1") is False
    await objs.put_result("topwear", "h1", b"masks")
    assert await objs.result_exists("topwear", "h1") is True
    assert await objs.get_result("topwear", "h1") == b"masks"


async def test_paths_follow_prefix_template():
    store = FakeObjectStore()
    objs = ObjectStoreInferenceObjects(store=store)  # default prefix
    await objs.put_request("m", "r1", b"x")
    assert await store.exists("AppData/m/requests/r1")  # default data prefix is AppData/
    custom = ObjectStoreInferenceObjects(store=store, prefix_template="custom/{app}")
    await custom.put_request("m", "r2", b"y")
    assert await store.exists("custom/m/requests/r2")  # explicit template still honored


async def test_get_result_missing_raises():
    objs = ObjectStoreInferenceObjects(store=FakeObjectStore())
    with pytest.raises(KeyNotFound):
        await objs.get_result("m", "nope")
