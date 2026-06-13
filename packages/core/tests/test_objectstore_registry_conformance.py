import pytest
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.testing.fakes import FakeObjectStore
from sluice_core.testing.registry_conformance import RegistryConformance


class TestObjectStoreRegistryConformance(RegistryConformance):
    @pytest.fixture
    def registry(self):
        return ObjectStoreAppRegistry(store=FakeObjectStore())
