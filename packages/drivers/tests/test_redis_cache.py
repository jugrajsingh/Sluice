import fakeredis.aioredis
import pytest
from sluice_core.testing.cache_conformance import CacheConformance
from sluice_drivers.redis_cache import RedisCache


class TestRedisCacheConformance(CacheConformance):
    @pytest.fixture
    def cache(self):
        return RedisCache(client=fakeredis.aioredis.FakeRedis())
