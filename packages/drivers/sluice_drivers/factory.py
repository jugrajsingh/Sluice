from __future__ import annotations

from sluice_core.config import ObjectStoreSettings, Settings
from sluice_core.interfaces import AppRegistry, Cache, ObjectStore, Queue


def build_queue(s: Settings, *, idle_reclaim_ms: int | None = None) -> Queue:
    """Build the configured Queue driver.

    ``idle_reclaim_ms`` overrides the driver's default lease/idle-reclaim window. The
    batch lane uses a LONG window (M3) so a multi-minute JSONL file is not reclaimed
    mid-process; the worker's heartbeat ``extend`` keeps the lease inside that window.
    """
    b = s.queue.backend
    o = s.queue.options
    if b == "redis":
        import redis.asyncio as aioredis

        from .redis_queue import RedisQueue

        client = aioredis.from_url(o.get("url", "redis://localhost:6379/0"))
        group = o.get("group", "sluice")
        consumer = o.get("consumer", "c1")
        if idle_reclaim_ms is not None:
            return RedisQueue(client=client, group=group, consumer=consumer, idle_reclaim_ms=idle_reclaim_ms)
        return RedisQueue(client=client, group=group, consumer=consumer)
    if b == "sqs":
        from .sqs_queue import SqsQueue

        return SqsQueue(region=o.get("region", "us-east-1"), endpoint_url=o.get("endpoint_url") or None)
    raise ValueError(f"unknown queue backend: {b}")


def _build_object_store_from(cfg: ObjectStoreSettings) -> ObjectStore:
    """Construct an ObjectStore driver from a single ObjectStoreSettings block."""
    b = cfg.backend
    o = cfg.options
    if b in ("s3", "minio"):
        from .s3_store import S3ObjectStore

        return S3ObjectStore(
            bucket=o["bucket"], region=o.get("region", "us-east-1"), endpoint_url=o.get("endpoint_url") or None
        )
    if b == "gcs":
        from .gcs_store import GcsObjectStore

        return GcsObjectStore(bucket=o["bucket"], endpoint=o.get("endpoint") or None)
    raise ValueError(f"unknown object_store backend: {b}")


def build_object_store(s: Settings) -> ObjectStore:
    """The data store (inference + batch objects, `AppData/` prefix)."""
    return _build_object_store_from(s.object_store)


def build_state_store(s: Settings) -> ObjectStore:
    """The control-plane state store (app specs, status, VM heartbeats + tracking ledger).

    `state_store` unset ⇒ inherit `object_store` so single-bucket deploys are unchanged (ADR-011).
    """
    return _build_object_store_from(s.state_store or s.object_store)


def build_registry(s: Settings, *, store: ObjectStore | None = None) -> AppRegistry:
    b = s.registry.backend
    if b == "objectstore":
        from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry

        return ObjectStoreAppRegistry(
            store=store or build_object_store(s), root=s.registry.options.get("root", "sluice")
        )
    raise ValueError(f"unknown registry backend: {b}")


def build_cache(s: Settings, *, store: ObjectStore | None = None) -> Cache:
    b = s.cache.backend
    o = s.cache.options
    if b == "redis":
        import redis.asyncio as aioredis

        from .redis_cache import RedisCache

        return RedisCache(client=aioredis.from_url(o.get("url", "redis://localhost:6379/0")))
    if b == "objectstore":
        from sluice_core.drivers.cache_objectstore import ObjectStoreCache

        return ObjectStoreCache(store=store or build_object_store(s), root=o.get("root", "sluice/cache"))
    raise ValueError(f"unknown cache backend: {b}")
