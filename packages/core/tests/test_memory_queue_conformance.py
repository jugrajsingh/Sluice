import pytest
from sluice_core.drivers.memory import MemoryQueue
from sluice_core.testing.queue_conformance import QueueConformance


class TestMemoryQueueConformance(QueueConformance):
    @pytest.fixture
    def queue(self):
        return MemoryQueue(default_lease_s=30)
