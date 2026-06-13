import pytest
from sluice_core.drivers.cache_objectstore import ObjectStoreCache
from sluice_core.testing.cache_conformance import CacheConformance
from sluice_core.testing.fakes import FakeObjectStore


class TestObjectStoreCacheConformance(CacheConformance):
    @pytest.fixture
    def cache(self):
        return ObjectStoreCache(store=FakeObjectStore())
