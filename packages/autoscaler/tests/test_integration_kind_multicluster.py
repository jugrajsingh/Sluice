"""Two-cluster integration test: Sluice in cluster A creates worker pods in cluster B.

Gated by SLUICE_KIND_MULTI=1. Requires a kubeconfig (KUBECONFIG) with two contexts — one per
kind cluster — named by SLUICE_KIND_CONTEXT_A and SLUICE_KIND_CONTEXT_B. The app targets cluster B
by name in its placement, so this exercises the multi-cluster pool routing end to end.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SLUICE_KIND_MULTI") != "1",
    reason="requires two kind clusters (set SLUICE_KIND_MULTI=1 + SLUICE_KIND_CONTEXT_A/_B)",
)


async def test_controller_creates_pods_in_external_cluster():
    from sluice_autoscaler.controller import Controller
    from sluice_autoscaler.k8s import KubeClusterInspector, KubePodManager
    from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
    from sluice_core.models import AppSpec, K8sPlacementSpec, KubernetesCandidate
    from sluice_core.testing.fakes import FakeObjectStore, FakeQueue

    ns = os.environ.get("SLUICE_KIND_NAMESPACE", "default")
    kubeconfig = os.environ.get("KUBECONFIG")
    ctx_a = os.environ["SLUICE_KIND_CONTEXT_A"]
    ctx_b = os.environ["SLUICE_KIND_CONTEXT_B"]
    image = os.environ.get("SLUICE_KIND_IMAGE", "busybox")
    model = os.environ.get("SLUICE_KIND_MODEL", "multi-model")

    def _kw(ctx: str) -> dict:
        return {"in_cluster": False, "config_path": kubeconfig, "context_name": ctx, "namespace": ns}

    pods_a, inspect_a = KubePodManager(**_kw(ctx_a)), KubeClusterInspector(**_kw(ctx_a))
    pods_b, inspect_b = KubePodManager(**_kw(ctx_b)), KubeClusterInspector(**_kw(ctx_b))
    handles = [pods_a, inspect_a, pods_b, inspect_b]
    for c in handles:
        await c.open()

    registry = ObjectStoreAppRegistry(store=FakeObjectStore())
    # placement targets cluster B by name only — nothing should land in cluster A
    app = AppSpec(
        name=model,
        image=image,
        handler="handler:H",
        placement=[KubernetesCandidate(provider="cluster-b", spec=K8sPlacementSpec(node_selectors=[{}]))],
    )
    await registry.put_app(app)

    queue = FakeQueue()
    for i in range(100):
        await queue.enqueue(model, f"job{i}".encode())

    controller = Controller(
        registry=registry,
        queue=queue,
        inspector=inspect_a,  # the in-cluster handle (cluster A)
        pods=pods_a,
        clusters={"cluster-b": (pods_b, inspect_b)},
    )
    try:
        await controller.reconcile_one(app)
        assert len(await inspect_b.workers(app)) >= 1, "expected pods created in the external cluster B"
        assert len(await inspect_a.workers(app)) == 0, "cluster A must stay empty"
    finally:
        for c in handles:
            await c.close()
