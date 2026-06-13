import uuid

import pytest
from sluice_core.testing.store_conformance import ObjectStoreConformance
from sluice_drivers.s3_store import S3ObjectStore


class TestS3Store(ObjectStoreConformance):
    @pytest.fixture
    async def store(self, moto_server):
        s = S3ObjectStore(
            bucket=f"b-{uuid.uuid4().hex}",
            region="us-east-1",
            endpoint_url=moto_server,
            access_key="test",
            secret_key="test",
        )
        await s.ensure_bucket()
        return s

    async def test_signed_url_method_changes_signature(self, store):
        await store.put("k", b"v")
        get_url = await store.signed_url("k", method="GET", expires_s=120)
        put_url = await store.signed_url("k", method="PUT", expires_s=120)
        assert "Signature" in put_url  # SigV2 (Signature=) or SigV4 (X-Amz-Signature=)
        assert get_url != put_url  # the HTTP method participates in the presign signature
