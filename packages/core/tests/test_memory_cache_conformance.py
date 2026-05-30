import pytest
from sluice_core.drivers.cache_memory import MemoryCache
from sluice_core.testing.cache_conformance import CacheConformance


class TestMemoryCacheConformance(CacheConformance):
    @pytest.fixture
    def cache(self):
        return MemoryCache()
