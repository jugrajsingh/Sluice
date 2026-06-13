import pytest
from pydantic import ValidationError
from sluice_core.models import (
    AppSpec,
    AppStatus,
    CandidateOverrides,
    K8sPlacementSpec,
    KubernetesCandidate,
    ServerSpec,
    Toleration,
    VmCandidate,
    VmPlacementSpec,
    WorkerSpec,
    WorkerState,
    WorkerStatus,
)


def test_worker_state_values():
    assert WorkerState.unschedulable.value == "unschedulable"
    assert WorkerState("running") is WorkerState.running


def test_worker_status_minimal():
    w = WorkerStatus(pod="p-1", state=WorkerState.pending)
    assert w.reason is None and w.age_s == 0 and w.restarts == 0
    assert w.candidate is None


def test_appspec_defaults_fill_from_name():
    app = AppSpec(name="topwear", image="repo/x:1")
    assert app.queue_ref == "topwear"
    assert app.storage_prefix == "apps/topwear"
    assert app.desired_state == "Ready"
    assert app.scaling.max_workers == 0  # 0 = unbounded
    assert app.scaling.messages_per_worker == 10
    # default placement: one in-cluster spot candidate that schedules anywhere
    assert len(app.placement) == 1
    cand = app.placement[0]
    assert cand.type == "kubernetes" and cand.provider == "in-cluster"
    assert cand.spec.pricing == "spot" and cand.spec.node_selectors == [{}]


def test_placement_is_ordered_discriminated_union():
    app = AppSpec(
        name="m",
        image="i",
        placement=[
            KubernetesCandidate(
                provider="in-cluster",
                spec=K8sPlacementSpec(
                    pricing="spot",
                    node_selectors=[{"gpu": "l4", "lifecycle": "spot"}, {"cloud.google.com/gke-spot": "true"}],
                    tolerations=[Toleration(key="nvidia.com/gpu")],
                    schedule_grace_s=120,
                ),
            ),
            VmCandidate(provider="gce", spec=VmPlacementSpec(pricing="spot", machine_type="g2", regions=["r1"])),
            KubernetesCandidate(provider="gke-east", spec=K8sPlacementSpec(pricing="on-demand")),
        ],
    )
    # order is preserved exactly (list index = priority)
    assert [c.type for c in app.placement] == ["kubernetes", "vm", "kubernetes"]
    assert [c.provider for c in app.placement] == ["in-cluster", "gce", "gke-east"]
    # ordered node selectors kept in author order
    assert app.placement[0].spec.node_selectors[0] == {"gpu": "l4", "lifecycle": "spot"}
    assert app.placement[0].spec.tolerations[0].effect == "NoSchedule"
    assert app.placement[0].spec.schedule_grace_s == 120
    assert app.placement[1].spec.machine_type == "g2" and app.placement[1].spec.workers_per_vm == 1
    assert app.placement[2].spec.pricing == "on-demand"


def test_appstatus_defaults():
    s = AppStatus()
    assert s.phase == "Ready" and s.reason is None and s.candidate is None
    assert s.workers == {} and s.queue.visible == 0


def test_worker_defaults_to_in_process_handler():
    app = AppSpec(name="m", image="i")
    assert app.worker.type == "handler" and app.worker.instances == 1
    assert app.worker.args == [] and app.worker.server is None


def test_sidecar_worker_requires_server_config():
    with pytest.raises(ValidationError):
        WorkerSpec(type="sidecar")  # no server
    w = WorkerSpec(type="sidecar", instances=3, server=ServerSpec(port=8080, request_path="/v1/segment"))
    assert w.server.port == 8080 and w.server.request_path == "/v1/segment"
    assert w.server.method == "POST" and w.server.health_path == "/healthz" and w.server.ready_timeout_s == 600


def test_server_spec_aliases_round_trip():
    s = ServerSpec.model_validate(
        {"port": 9000, "requestPath": "/infer", "contentType": "application/json", "readyTimeoutS": 300}
    )
    assert s.request_path == "/infer" and s.content_type == "application/json" and s.ready_timeout_s == 300
    assert s.model_dump(by_alias=True)["requestPath"] == "/infer"


def test_candidate_overrides_optional_and_partial():
    cand = KubernetesCandidate(
        provider="in-cluster",
        spec=K8sPlacementSpec(pricing="spot"),
        overrides=CandidateOverrides(instances=6, env={"SERVER__WORKERS": "6"}),
    )
    assert cand.overrides.instances == 6 and cand.overrides.env == {"SERVER__WORKERS": "6"}
    assert cand.overrides.image is None and cand.overrides.args is None
    # default: no overrides
    assert VmCandidate(provider="gce", spec=VmPlacementSpec(machine_type="g2")).overrides is None
