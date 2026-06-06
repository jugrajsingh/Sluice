from sluice_autoscaler.k8s import build_worker_pod
from sluice_core.models import AppSpec, ResourcesSpec


def test_build_worker_pod_from_spec():
    app = AppSpec(name="m", image="repo/x:1", handler="h:H", resources=ResourcesSpec(gpu=1, cpu=2, memory_gb=8))
    pod = build_worker_pod(app, selector={"pool": "spot"}, backend_env={"QUEUE__BACKEND": "redis"}, namespace="sluice")
    assert pod.spec.restart_policy == "OnFailure"
    assert pod.spec.node_selector == {"pool": "spot"}
    c = pod.spec.containers[0]
    assert c.image == "repo/x:1"
    assert c.resources.limits["nvidia.com/gpu"] == "1"
    names = {e.name: e.value for e in c.env}
    assert names["WORKER__APP"] == "m" and names["QUEUE__BACKEND"] == "redis"


def test_candidate_annotation_set_when_given():
    app = AppSpec(name="m", image="i", handler="h:H")
    pod = build_worker_pod(
        app,
        selector={},
        backend_env={},
        namespace="ns",
        candidate_key="kubernetes/k8s/z1/l4/spot",  # gitleaks:allow (placement key, not a secret)
    )
    assert pod.metadata.annotations == {"sluice.jugraj.dev/candidate": "kubernetes/k8s/z1/l4/spot"}
