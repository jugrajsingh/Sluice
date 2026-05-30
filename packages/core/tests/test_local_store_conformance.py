import pytest
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.testing.store_conformance import ObjectStoreConformance


class TestLocalStoreConformance(ObjectStoreConformance):
    @pytest.fixture
    def store(self, tmp_path):
        return LocalObjectStore(root=str(tmp_path))
