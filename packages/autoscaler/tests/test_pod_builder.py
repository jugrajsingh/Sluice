from sluice_autoscaler.k8s import build_worker_pod
from sluice_core.auth import verify_worker_token
from sluice_core.models import AppSpec, ResourcesSpec

KEY = "autoscaler-signing-key"  # gitleaks:allow (test fixture, not a secret)


def test_worker_pod_broker_env_valid_token_no_backend_creds():
    app = AppSpec(name="seg", image="repo/x:1", handler="h:H", resources=ResourcesSpec(gpu=1, cpu=2, memory_gb=8))
    pod = build_worker_pod(
        app,
        selector={"pool": "spot"},
        namespace="sluice",
        broker_url="http://sluice-gateway",
        signing_key=KEY,
        worker_id="seg-abc123",
    )
    assert pod.spec.restart_policy == "OnFailure"
    assert pod.spec.node_selector == {"pool": "spot"}
    c = pod.spec.containers[0]
    assert c.image == "repo/x:1"
    assert c.resources.limits["nvidia.com/gpu"] == "1"
    env = {e.name: e.value for e in c.env}
    assert env["WORKER__BROKER_URL"] == "http://sluice-gateway"
    assert env["WORKER__APP"] == "seg"
    assert "QUEUE__BACKEND" not in env and "OBJECT_STORE__BACKEND" not in env
    claims = verify_worker_token(env["WORKER__BROKER_TOKEN"], key=KEY)
    assert claims["app"] == "seg" and claims["worker_id"] == "seg-abc123"


def test_candidate_annotation_set_when_given():
    app = AppSpec(name="m", image="i", handler="h:H")
    pod = build_worker_pod(
        app,
        selector={},
        namespace="ns",
        broker_url="http://g",
        signing_key=KEY,
        worker_id="m-1",
        candidate_key="kubernetes/k8s/z1/l4/spot",  # gitleaks:allow (placement key, not a secret)
    )
    assert pod.metadata.annotations == {"sluice.jugraj.dev/candidate": "kubernetes/k8s/z1/l4/spot"}
