"""The in-process test doubles must satisfy the SAME interface contract as the real
drivers — so they can never drift from `Queue`/`ObjectStore` without failing CI."""

import pytest
from sluice_core.testing.fakes import FakeObjectStore, FakeQueue
from sluice_core.testing.queue_conformance import QueueConformance
from sluice_core.testing.store_conformance import ObjectStoreConformance


class TestFakeQueueConformance(QueueConformance):
    @pytest.fixture
    def queue(self):
        return FakeQueue(default_lease_s=30)


class TestFakeObjectStoreConformance(ObjectStoreConformance):
    @pytest.fixture
    def store(self):
        return FakeObjectStore()
