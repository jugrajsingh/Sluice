import uuid

import pytest
from sluice_core.testing.queue_conformance import QueueConformance
from sluice_drivers.sqs_queue import SqsQueue


class TestSqsQueue(QueueConformance):
    @pytest.fixture
    def source(self):
        # Unique queue name per test: the session-scoped moto server is shared,
        # so a fixed name would leak in-flight/visible state across tests and
        # pollute the approximate depth counts.
        return f"conformance-{uuid.uuid4().hex}"

    @pytest.fixture
    async def queue(self, moto_server, source):
        q = SqsQueue(
            region="us-east-1", endpoint_url=moto_server, access_key="test", secret_key="test", default_lease_s=1
        )
        await q.ensure_queue(source)
        return q
