import pytest
from sluice_core.drivers.cache_objectstore import ObjectStoreCache
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.testing.cache_conformance import CacheConformance


class TestObjectStoreCacheConformance(CacheConformance):
    @pytest.fixture
    def cache(self, tmp_path):
        return ObjectStoreCache(store=LocalObjectStore(root=str(tmp_path)))
