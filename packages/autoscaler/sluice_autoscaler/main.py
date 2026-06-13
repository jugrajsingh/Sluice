"""Autoscaler entrypoint: leader-elected reconcile loop over the spec store.

Composes a `Queue` (via `sluice_drivers.factory`), an `AppRegistry` (spec store),
`KubePodManager`, and `KubeClusterInspector` into a `Controller`, then reconciles
every App each cycle. Only the holder of a `coordination.k8s.io` Lease
reconciles, so HA replicas never double-create pods. Each cycle has a deadline;
failures back off exponentially (capped at 300 s). Exposes `/healthz` + `/metrics`.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import UTC

from kubernetes_asyncio import config
from kubernetes_asyncio.client import ApiClient, CoordinationV1Api
from kubernetes_asyncio.client.rest import ApiException
from sluice_core.config import Settings
from sluice_core.interfaces import AppRegistry
from sluice_drivers.factory import build_queue

from .controller import Controller
from .k8s import KubeClusterInspector, KubePodManager

CYCLE_SECONDS = int(os.getenv("AUTOSCALER__CYCLE_SECONDS", "15"))
CYCLE_DEADLINE_SECONDS = int(os.getenv("AUTOSCALER__CYCLE_DEADLINE_SECONDS", "60"))
BACKOFF_CAP_SECONDS = 300
LEASE_NAME = os.getenv("AUTOSCALER__LEASE_NAME", "sluice-autoscaler")
LEASE_NAMESPACE = os.getenv("AUTOSCALER__LEASE_NAMESPACE", "default")
IDENTITY = os.getenv("POD_NAME", socket.gethostname())


async def _try_acquire_lease(coord: CoordinationV1Api) -> bool:
    """Best-effort leader election: own the Lease if unheld, ours, or STALE.

    A lease whose renew_time is older than its lease_duration_seconds belongs to a
    dead holder (e.g. the pre-restart pod) and is taken over — otherwise a restarted
    controller would stay passive forever.
    """
    from datetime import datetime

    from kubernetes_asyncio.client import V1Lease, V1LeaseSpec, V1ObjectMeta

    now = datetime.now(UTC)
    try:
        lease = await coord.read_namespaced_lease(name=LEASE_NAME, namespace=LEASE_NAMESPACE)
        holder = lease.spec.holder_identity
        duration = lease.spec.lease_duration_seconds or max(CYCLE_SECONDS * 3, 30)
        renew = lease.spec.renew_time
        stale = renew is None or (now - renew).total_seconds() > duration
        if holder in (None, IDENTITY) or stale:
            if holder != IDENTITY:
                lease.spec.acquire_time = now
            lease.spec.holder_identity = IDENTITY
            lease.spec.renew_time = now
            lease.spec.lease_duration_seconds = duration
            await coord.replace_namespaced_lease(name=LEASE_NAME, namespace=LEASE_NAMESPACE, body=lease)
            return True
        return False
    except ApiException as e:
        if e.status != 404:
            raise
        body = V1Lease(
            metadata=V1ObjectMeta(name=LEASE_NAME, namespace=LEASE_NAMESPACE),
            spec=V1LeaseSpec(
                holder_identity=IDENTITY,
                acquire_time=now,
                renew_time=now,
                lease_duration_seconds=max(CYCLE_SECONDS * 3, 30),
            ),
        )
        await coord.create_namespaced_lease(namespace=LEASE_NAMESPACE, body=body)
        return True


def _gce_token() -> str:
    import json as _json
    import urllib.request

    tok = os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN")
    if tok:
        return tok
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed metadata URL
        return _json.loads(r.read())["access_token"]


def _build_prober(settings: Settings):
    kind = os.getenv("AUTOSCALER__PROBER", "")
    if kind == "gce":
        from .probers import GceStateProber

        return GceStateProber(project=settings.placement.provider_defaults.get("project", ""), token_getter=_gce_token)
    if kind == "ec2":
        from .probers import Ec2StateProber

        return Ec2StateProber(region=os.getenv("AWS_REGION", "us-east-1"))
    return None


async def _reconcile_cycle(controller: Controller, registry: AppRegistry) -> None:
    apps = await registry.list_apps()
    for app in apps:
        await controller.reconcile_one(app)


async def run() -> None:
    settings = Settings()
    in_cluster = os.getenv("AUTOSCALER__IN_CLUSTER", "1") == "1"
    workers_ns = os.getenv("AUTOSCALER__WORKERS_NAMESPACE", "default")
    kube_kw = {"in_cluster": in_cluster, "config_path": os.getenv("KUBECONFIG"), "namespace": workers_ns}
    broker_url = os.getenv("AUTOSCALER__BROKER_URL", "http://sluice-gateway")
    signing_key = os.getenv("AUTOSCALER__SIGNING_KEY", "")

    from sluice_drivers.factory import build_cache, build_object_store, build_registry

    store = build_object_store(settings)
    registry = build_registry(settings, store=store)
    cache = build_cache(settings, store=store)
    pods = KubePodManager(broker_url=broker_url, signing_key=signing_key, **kube_kw)
    inspector = KubeClusterInspector(**kube_kw)
    for c in (pods, inspector):
        await c.open()

    # External clusters: AUTOSCALER__CLUSTERS=[{"name","kubeconfig_path","namespace"?}, ...].
    # Each gets its own kubeconfig-backed pod manager + inspector; apps target them by name in
    # a placement candidate's `provider`. Kubeconfigs are mounted Secrets (deployment-level).
    extra_clusters: dict[str, tuple[KubePodManager, KubeClusterInspector]] = {}
    extra_handles: list = []
    for entry in json.loads(os.getenv("AUTOSCALER__CLUSTERS", "") or "[]"):
        ckw = {
            "in_cluster": False,
            "config_path": entry["kubeconfig_path"],
            "namespace": entry.get("namespace", workers_ns),
        }
        cpods = KubePodManager(broker_url=broker_url, signing_key=signing_key, **ckw)
        cinsp = KubeClusterInspector(**ckw)
        extra_clusters[entry["name"]] = (cpods, cinsp)
        extra_handles += [cpods, cinsp]
    for c in extra_handles:
        await c.open()

    compute = None
    if settings.placement.tf_state_backend:
        from .terraform import TerraformProvider

        compute = TerraformProvider(
            module_dir=settings.placement.tf_module_dir,
            work_root=settings.placement.tf_work_root,
            state_backend=settings.placement.tf_state_backend,
            provider_defaults=settings.placement.provider_defaults,
            broker_url=broker_url,
            signing_key=signing_key,
            prober=_build_prober(settings),
        )

    from .vm_commands import VmCommander

    controller = Controller(
        registry=registry,
        queue=build_queue(settings),
        inspector=inspector,
        pods=pods,
        clusters=extra_clusters,
        compute=compute,
        commander=VmCommander(store=store),
        cache=cache,
        store=store,
        stockout_ttl_s=settings.placement.stockout_ttl_s,
        boot_deadline_s=settings.placement.boot_deadline_s,
    )

    from .http import start_health_server

    http_runner = await start_health_server(int(os.getenv("AUTOSCALER__HTTP_PORT", "8081")))

    if in_cluster:
        config.load_incluster_config()
    else:
        await config.load_kube_config(config_file=os.getenv("KUBECONFIG"))

    backoff = CYCLE_SECONDS
    try:
        async with ApiClient() as api:
            coord = CoordinationV1Api(api)
            while True:
                try:
                    if await _try_acquire_lease(coord):
                        await asyncio.wait_for(_reconcile_cycle(controller, registry), timeout=CYCLE_DEADLINE_SECONDS)
                    backoff = CYCLE_SECONDS
                except Exception as e:  # noqa: BLE001 - loop must survive transient API errors
                    print(f"reconcile cycle failed (retry in {backoff}s): {e!r}", flush=True)
                    backoff = min(backoff * 2, BACKOFF_CAP_SECONDS)
                await asyncio.sleep(backoff)
    finally:
        await http_runner.cleanup()
        for c in (pods, inspector, *extra_handles):
            await c.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
