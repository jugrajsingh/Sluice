from sluice_autoscaler.k8s import build_worker_pod
from sluice_core.auth import verify_worker_token
from sluice_core.models import AppSpec, ResourcesSpec, ServerSpec, Toleration

KEY = "autoscaler-signing-key"  # gitleaks:allow (test fixture, not a secret)


def _tols(pod):
    return [(t.key, t.operator, t.effect) for t in (pod.spec.tolerations or [])]


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


def test_gpu_pod_gets_default_gpu_toleration():
    app = AppSpec(name="seg", image="i", handler="h:H", resources=ResourcesSpec(gpu=1))
    pod = build_worker_pod(
        app, selector={"gpu": "l4"}, namespace="ns", broker_url="http://g", signing_key=KEY, worker_id="seg-1"
    )
    assert ("nvidia.com/gpu", "Exists", "NoSchedule") in _tols(pod)


def test_non_gpu_pod_has_no_gpu_limit_or_toleration():
    app = AppSpec(name="cpu", image="i", handler="h:H", resources=ResourcesSpec(gpu=0))
    pod = build_worker_pod(app, selector={}, namespace="ns", broker_url="http://g", signing_key=KEY, worker_id="cpu-1")
    assert "nvidia.com/gpu" not in pod.spec.containers[0].resources.limits
    assert _tols(pod) == []  # no taints to tolerate


def test_explicit_tolerations_passed_through_without_duplicating_gpu_default():
    app = AppSpec(name="seg", image="i", handler="h:H", resources=ResourcesSpec(gpu=1))
    custom = [
        Toleration(key="dedicated", operator="Equal", value="ml", effect="NoSchedule"),
        Toleration(key="nvidia.com/gpu", operator="Exists", effect="NoSchedule"),
    ]
    pod = build_worker_pod(
        app,
        selector={},
        namespace="ns",
        broker_url="http://g",
        signing_key=KEY,
        worker_id="seg-2",
        tolerations=custom,
    )
    tols = _tols(pod)
    assert ("dedicated", "Equal", "NoSchedule") in tols
    assert tols.count(("nvidia.com/gpu", "Exists", "NoSchedule")) == 1  # not duplicated


def test_candidate_annotation_set_when_given():
    app = AppSpec(name="m", image="i", handler="h:H")
    pod = build_worker_pod(
        app,
        selector={},
        namespace="ns",
        broker_url="http://g",
        signing_key=KEY,
        worker_id="m-1",
        candidate_key="kubernetes/in-cluster/any/none/l4/spot",  # gitleaks:allow (placement key, not a secret)
    )
    assert pod.metadata.annotations == {"sluice.jugraj.dev/candidate": "kubernetes/in-cluster/any/none/l4/spot"}


def _env(container):
    return {e.name: e.value for e in (container.env or [])}


def test_handler_pod_packs_with_launcher_when_instances_gt_1():
    app = AppSpec(name="seg", image="i", handler="h:H", resources=ResourcesSpec(gpu=1))
    pod = build_worker_pod(
        app, selector={}, namespace="ns", broker_url="http://g", signing_key=KEY, worker_id="seg-1", instances=3
    )
    assert len(pod.spec.containers) == 1
    cmd = pod.spec.containers[0].command
    assert cmd[:3] == ["python", "-m", "sluice_worker.launch"] and "--instances" in cmd and "3" in cmd
    assert pod.spec.containers[0].resources.limits["nvidia.com/gpu"] == "1"  # the one container owns the GPU


def test_sidecar_pod_has_server_and_adapter_containers():
    app = AppSpec(name="seg", image="samserve:1", handler="h:H", resources=ResourcesSpec(gpu=1, cpu=4, memory_gb=20))
    server = ServerSpec(port=8080, request_path="/v1/segment", health_path="/healthz", ready_timeout_s=600)
    pod = build_worker_pod(
        app,
        selector={"gpu": "l4"},
        namespace="ns",
        broker_url="http://g",
        signing_key=KEY,
        worker_id="seg-2",
        worker_type="sidecar",
        instances=3,
        env={"MODEL__VARIANT": "sam3.1"},
        args=["--flag"],
        server=server,
    )
    by_name = {c.name: c for c in pod.spec.containers}
    assert set(by_name) == {"server", "worker"}
    # model server: owns the GPU, runs its own entrypoint (+args), has the model env, NO broker token
    srv = by_name["server"]
    assert srv.image == "samserve:1" and srv.args == ["--flag"]
    assert srv.resources.limits["nvidia.com/gpu"] == "1"
    assert srv.startup_probe is not None and srv.startup_probe.http_get.path == "/healthz"
    srv_env = _env(srv)
    assert srv_env["MODEL__VARIANT"] == "sam3.1" and "WORKER__BROKER_TOKEN" not in srv_env
    # adapter: holds the JWT + server config, requests no GPU
    ad = by_name["worker"]
    assert ad.command == ["python", "-m", "sluice_worker.adapter"]
    ad_env = _env(ad)
    assert ad_env["WORKER__SERVER_PORT"] == "8080" and ad_env["WORKER__SERVER_REQUEST_PATH"] == "/v1/segment"
    assert ad_env["WORKER__CONCURRENCY"] == "3"
    assert verify_worker_token(ad_env["WORKER__BROKER_TOKEN"], key=KEY)["app"] == "seg"
    assert not (ad.resources and ad.resources.limits and "nvidia.com/gpu" in ad.resources.limits)
