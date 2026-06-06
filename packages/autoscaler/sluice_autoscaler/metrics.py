from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

RECONCILE_SECONDS = Histogram("sluice_reconcile_seconds", "Reconcile cycle duration", registry=REGISTRY)
SCALE_UP_PODS = Counter("sluice_scale_up_pods_total", "Pods created by scale-up", ["app"], registry=REGISTRY)
HOLDS = Counter("sluice_holds_total", "Reconciles that ended HOLD", ["app"], registry=REGISTRY)
WORKERS = Gauge("sluice_workers", "Observed workers by state", ["app", "state"], registry=REGISTRY)
STOCKOUTS = Counter("sluice_stockouts_total", "Stockout marks", ["substrate", "pricing"], registry=REGISTRY)
VMS = Gauge("sluice_vms", "Burst VMs by state", ["app", "state"], registry=REGISTRY)


def render() -> bytes:
    return generate_latest(REGISTRY)
