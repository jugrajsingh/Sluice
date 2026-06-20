import pytest
from sluice_drivers.gcs_store import GcsObjectStore


def test_gcs_store_construction_does_not_require_running_loop():
    """Regression: the gcloud.aio Storage client builds an aiohttp connector in __init__, which
    calls asyncio.get_running_loop(). The gateway/console construct the object store at module
    import time (before uvicorn starts the event loop), so construction MUST be lazy — otherwise
    they crash on startup with 'RuntimeError: no running event loop'. This sync test (no loop)
    reproduces that import-time scenario."""
    store = GcsObjectStore(bucket="b")
    assert store is not None


@pytest.mark.asyncio
async def test_gcs_emulator_returns_plain_url():
    store = GcsObjectStore(bucket="b", endpoint="http://localhost:4443")
    url = await store.signed_url("k", method="GET", expires_s=60)
    assert url and "localhost:4443" in url  # plain emulator URL, no real V4 signing


@pytest.mark.asyncio
async def test_gcs_signed_url_v4(monkeypatch):
    captured = {}

    class FakeBlob:
        def __init__(self, name):
            captured["name"] = name

        def generate_signed_url(self, *, version, method, expiration, **kw):
            captured.update(version=version, method=method)
            return f"https://storage.googleapis.com/signed/{method}"

    class FakeBucket:
        def blob(self, name):
            return FakeBlob(name)

    class FakeClient:
        def bucket(self, name):
            return FakeBucket()

    store = GcsObjectStore(bucket="b")
    monkeypatch.setattr(store, "_signing_client", lambda: FakeClient())
    url = await store.signed_url("apps/seg/results/r1", method="PUT", expires_s=120)
    assert captured["version"] == "v4"
    assert captured["method"] == "PUT"
    assert captured["name"] == "apps/seg/results/r1"
    assert url.endswith("/PUT")
