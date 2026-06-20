import pytest
from sluice_core.config import ObjectStoreSettings, Settings
from sluice_core.interfaces import AppRegistry, Cache, ObjectStore, Queue
from sluice_core.testing.fakes import FakeObjectStore
from sluice_drivers.factory import (
    build_cache,
    build_object_store,
    build_queue,
    build_registry,
    build_state_store,
)


def test_should_fall_back_to_object_store_when_state_store_unset():
    s = Settings()
    s.object_store.options = {"bucket": "data"}
    assert s.state_store is None
    # None ⇒ the state store IS the data store (single-bucket backward compat).
    assert isinstance(build_state_store(s), ObjectStore)
    assert build_state_store(s)._bucket == "data"


def test_should_build_separate_state_store_when_configured():
    s = Settings()
    s.object_store.options = {"bucket": "data"}
    s.state_store = ObjectStoreSettings(backend="s3", options={"bucket": "sluice-state"})
    st = build_state_store(s)
    assert isinstance(st, ObjectStore)
    assert st._bucket == "sluice-state"  # distinct config honored, not the data bucket
    assert build_object_store(s)._bucket == "data"  # data store unaffected


def test_default_backends_build():
    s = Settings()  # queue=redis, object_store=s3
    assert isinstance(build_queue(s), Queue)
    s.object_store.options = {"bucket": "b"}
    assert isinstance(build_object_store(s), ObjectStore)


def test_should_apply_long_idle_window_when_building_batch_queue():
    """build_queue honors an explicit idle_reclaim_ms so a long batch lease window
    can be configured for the {app}-batch queue (M3)."""
    s = Settings()  # queue=redis
    q = build_queue(s, idle_reclaim_ms=900_000)
    assert isinstance(q, Queue)
    assert q._idle == 900_000


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
