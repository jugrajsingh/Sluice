from __future__ import annotations

from sluice_core.config import Settings
from sluice_core.drivers.local_store import LocalObjectStore
from sluice_core.drivers.memory import MemoryQueue
from sluice_core.interfaces import AppRegistry, Cache, ObjectStore, Queue


def build_queue(s: Settings) -> Queue:
    b = s.queue.backend
    o = s.queue.options
    if b == "memory":
        return MemoryQueue()
    if b == "redis":
        import redis.asyncio as aioredis

        from .redis_queue import RedisQueue

        return RedisQueue(
            client=aioredis.from_url(o.get("url", "redis://localhost:6379/0")),
            group=o.get("group", "sluice"),
            consumer=o.get("consumer", "c1"),
        )
    if b == "sqs":
        from .sqs_queue import SqsQueue

        return SqsQueue(region=o.get("region", "us-east-1"), endpoint_url=o.get("endpoint_url") or None)
    raise ValueError(f"unknown queue backend: {b}")


def build_object_store(s: Settings) -> ObjectStore:
    b = s.object_store.backend
    o = s.object_store.options
    if b == "local":
        return LocalObjectStore(root=o.get("root", "/tmp/sluice"))
    if b in ("s3", "minio"):
        from .s3_store import S3ObjectStore

        return S3ObjectStore(
            bucket=o["bucket"], region=o.get("region", "us-east-1"), endpoint_url=o.get("endpoint_url") or None
        )
    if b == "gcs":
        from .gcs_store import GcsObjectStore

        return GcsObjectStore(bucket=o["bucket"], endpoint=o.get("endpoint") or None)
    raise ValueError(f"unknown object_store backend: {b}")


def build_registry(s: Settings, *, store: ObjectStore | None = None) -> AppRegistry:
    b = s.registry.backend
    if b == "memory":
        from sluice_core.drivers.registry_memory import MemoryAppRegistry

        return MemoryAppRegistry()
    if b == "objectstore":
        from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry

        return ObjectStoreAppRegistry(
            store=store or build_object_store(s), root=s.registry.options.get("root", "sluice")
        )
    raise ValueError(f"unknown registry backend: {b}")


def build_cache(s: Settings, *, store: ObjectStore | None = None) -> Cache:
    b = s.cache.backend
    o = s.cache.options
    if b == "memory":
        from sluice_core.drivers.cache_memory import MemoryCache

        return MemoryCache()
    if b == "redis":
        import redis.asyncio as aioredis

        from .redis_cache import RedisCache

        return RedisCache(client=aioredis.from_url(o.get("url", "redis://localhost:6379/0")))
    if b == "objectstore":
        from sluice_core.drivers.cache_objectstore import ObjectStoreCache

        return ObjectStoreCache(store=store or build_object_store(s), root=o.get("root", "sluice/cache"))
    raise ValueError(f"unknown cache backend: {b}")
