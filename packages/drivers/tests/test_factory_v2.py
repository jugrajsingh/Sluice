from sluice_core.config import Settings
from sluice_core.interfaces import AppRegistry, Cache
from sluice_drivers.factory import build_cache, build_registry


def test_registry_memory_and_objectstore(tmp_path):
    s = Settings()
    s.registry.backend = "memory"
    assert isinstance(build_registry(s), AppRegistry)
    s.registry.backend = "objectstore"
    s.object_store.backend = "local"
    s.object_store.options = {"root": str(tmp_path)}
    assert isinstance(build_registry(s), AppRegistry)


def test_cache_backends(tmp_path):
    s = Settings()
    assert isinstance(build_cache(s), Cache)  # memory default
    s.cache.backend = "objectstore"
    s.object_store.backend = "local"
    s.object_store.options = {"root": str(tmp_path)}
    assert isinstance(build_cache(s), Cache)
