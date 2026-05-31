import pytest
from sluice_core.config import Settings
from sluice_core.interfaces import ObjectStore, Queue
from sluice_drivers.factory import build_object_store, build_queue


def test_memory_and_local_defaults():
    s = Settings()
    assert isinstance(build_queue(s), Queue)
    assert isinstance(build_object_store(s), ObjectStore)


def test_unknown_backend_raises():
    s = Settings()
    s.queue.backend = "nope"
    with pytest.raises(ValueError, match="nope"):
        build_queue(s)
