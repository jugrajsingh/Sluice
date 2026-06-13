import pytest
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.errors import KeyNotFound, SigningUnsupported


async def test_put_get_roundtrip(tmp_path):
    s = LocalObjectStore(root=str(tmp_path))
    await s.put("a/b.txt", b"data")
    assert await s.get("a/b.txt") == b"data"
    assert await s.exists("a/b.txt") is True


async def test_get_missing_raises(tmp_path):
    s = LocalObjectStore(root=str(tmp_path))
    with pytest.raises(KeyNotFound):
        await s.get("nope")


async def test_signed_url_raises_unsupported(tmp_path):
    s = LocalObjectStore(root=str(tmp_path))
    await s.put("k", b"x")
    with pytest.raises(SigningUnsupported):
        await s.signed_url("k", expires_s=60)
