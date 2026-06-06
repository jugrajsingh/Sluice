from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, generate_latest

REGISTRY = CollectorRegistry()

CACHE_HITS = Counter("sluice_gateway_cache_hits_total", "Results served from cache", ["app"], registry=REGISTRY)
SYNC_HITS = Counter("sluice_gateway_sync_hits_total", "Sync-sugar 200s within t_sync", ["app"], registry=REGISTRY)
ENQUEUES = Counter("sluice_gateway_enqueues_total", "Jobs enqueued", ["app"], registry=REGISTRY)


def render() -> bytes:
    return generate_latest(REGISTRY)
