import pytest
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
from sluice_core.testing.registry_conformance import RegistryConformance


class TestObjectStoreRegistryConformance(RegistryConformance):
    @pytest.fixture
    def registry(self, tmp_path):
        return ObjectStoreAppRegistry(store=LocalObjectStore(root=str(tmp_path)))
