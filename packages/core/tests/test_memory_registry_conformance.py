import pytest
from sluice_core.drivers.registry_memory import MemoryAppRegistry
from sluice_core.testing.registry_conformance import RegistryConformance


class TestMemoryRegistryConformance(RegistryConformance):
    @pytest.fixture
    def registry(self):
        return MemoryAppRegistry()
