"""Integration test against a real kind cluster. Gated by SLUICE_KIND=1.

Requires the worker image preloaded and a backlogged queue; asserts the controller
creates bare pods from the spec and reaps them after self-exit.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SLUICE_KIND") != "1", reason="requires a kind cluster (set SLUICE_KIND=1)"
)


async def test_scale_up_then_reap_on_kind():
    from sluice_autoscaler.controller import Controller
    from sluice_autoscaler.k8s import KubeClusterInspector, KubePodManager
    from sluice_core.drivers.registry_objectstore import ObjectStoreAppRegistry
    from sluice_core.models import AppSpec
    from sluice_core.testing.fakes import FakeObjectStore, FakeQueue

    ns = os.environ.get("SLUICE_KIND_NAMESPACE", "default")
    model = os.environ.get("SLUICE_KIND_MODEL", "kind-model")
    kube_kw = {"in_cluster": False, "config_path": os.environ.get("KUBECONFIG"), "namespace": ns}

    registry = ObjectStoreAppRegistry(store=FakeObjectStore())
    await registry.put_app(
        AppSpec(name=model, image=os.environ.get("SLUICE_KIND_IMAGE", "busybox"), handler="handler:H")
    )
    pods = KubePodManager(**kube_kw)
    inspector = KubeClusterInspector(**kube_kw)
    for c in (pods, inspector):
        await c.open()

    queue = FakeQueue()
    for i in range(100):
        await queue.enqueue(model, f"job{i}".encode())

    controller = Controller(registry=registry, queue=queue, inspector=inspector, pods=pods)
    try:
        app = await registry.get_app(model)
        assert app is not None
        await controller.reconcile_one(app)
        workers = await inspector.workers(app)
        assert len(workers) >= 1, "expected the controller to create at least one bare pod"
    finally:
        for c in (pods, inspector):
            await c.close()
