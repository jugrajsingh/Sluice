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
