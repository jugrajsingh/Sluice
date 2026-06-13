import pytest
from sluice_core.config import Settings
from sluice_core.interfaces import AppRegistry, Cache, ObjectStore, Queue
from sluice_core.testing.fakes import FakeObjectStore
from sluice_drivers.factory import build_cache, build_object_store, build_queue, build_registry


def test_default_backends_build():
    s = Settings()  # queue=redis, object_store=s3
    assert isinstance(build_queue(s), Queue)
    s.object_store.options = {"bucket": "b"}
    assert isinstance(build_object_store(s), ObjectStore)


def test_unknown_backend_raises():
    s = Settings()
    s.queue.backend = "nope"
    with pytest.raises(ValueError, match="nope"):
        build_queue(s)


def test_removed_queue_and_store_backends_raise():
    s = Settings()
    s.queue.backend = "memory"
    with pytest.raises(ValueError, match="memory"):
        build_queue(s)
    s.object_store.backend = "local"
    with pytest.raises(ValueError, match="local"):
        build_object_store(s)


def test_registry_objectstore_builds():
    s = Settings()  # registry=objectstore
    assert isinstance(build_registry(s, store=FakeObjectStore()), AppRegistry)


def test_registry_memory_removed():
    s = Settings()
    s.registry.backend = "memory"
    with pytest.raises(ValueError, match="memory"):
        build_registry(s, store=FakeObjectStore())


def test_cache_objectstore_builds():
    s = Settings()  # cache=objectstore default
    assert isinstance(build_cache(s, store=FakeObjectStore()), Cache)


def test_cache_memory_removed():
    s = Settings()
    s.cache.backend = "memory"
    with pytest.raises(ValueError, match="memory"):
        build_cache(s, store=FakeObjectStore())
