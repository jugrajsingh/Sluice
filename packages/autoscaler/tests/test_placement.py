from sluice_autoscaler.placement import _selector_hash, candidate_key, expand_candidates
from sluice_core.models import (
    AppSpec,
    CandidateOverrides,
    K8sPlacementSpec,
    KubernetesCandidate,
    ResourcesSpec,
    ServerSpec,
    Toleration,
    VmCandidate,
    VmPlacementSpec,
    WorkerSpec,
)


def _app(placement=None):
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="nvidia-l4"),
        placement=placement
        if placement is not None
        else [
            KubernetesCandidate(
                provider="in-cluster",
                spec=K8sPlacementSpec(
                    pricing="spot",
                    node_selectors=[{"gke-nodepool": "l4-spot"}, {"gke-spot": "true"}],
                    tolerations=[Toleration(key="nvidia.com/gpu")],
                    schedule_grace_s=120,
                ),
            ),
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(
                    pricing="spot", machine_type="g2-standard-8", regions=["us-central1", "europe-west3"]
                ),
            ),
            KubernetesCandidate(
                provider="gke-east",
                spec=K8sPlacementSpec(pricing="on-demand", node_selectors=[{"pool": "od"}]),
            ),
        ],
    )


def test_expansion_preserves_author_order_across_mixed_candidates():
    cs = expand_candidates(_app())
    # k8s candidate -> one per selector; vm -> one per region; on-demand k8s last
    assert [(c.type, c.cluster, c.pricing) for c in cs] == [
        ("kubernetes", "in-cluster", "spot"),
        ("kubernetes", "in-cluster", "spot"),
        ("vm", "gce", "spot"),
        ("vm", "gce", "spot"),
        ("kubernetes", "gke-east", "on-demand"),
    ]
    # ordered node selectors: targeted pool first, broader fallback second
    assert cs[0].selector == {"gke-nodepool": "l4-spot"} and cs[1].selector == {"gke-spot": "true"}
    # vm candidates carry their region; k8s carry tolerations + grace
    assert [c.location for c in cs if c.type == "vm"] == ["us-central1", "europe-west3"]
    assert cs[0].tolerations[0].key == "nvidia.com/gpu" and cs[0].schedule_grace_s == 120


def test_candidate_key_includes_cluster_selector_gpu_pricing():
    cs = expand_candidates(_app())
    # distinct selectors in the same cluster get distinct keys (separate stockouts)
    assert candidate_key(cs[0]) != candidate_key(cs[1])
    assert candidate_key(cs[0]) == f"kubernetes/in-cluster/any/{_selector_hash(cs[0].selector)}/nvidia-l4/spot"
    assert candidate_key(cs[2]) == "vm/gce/us-central1/none/nvidia-l4/spot"
    # external cluster shows up in the key so it stocks out independently
    assert candidate_key(cs[4]).startswith("kubernetes/gke-east/any/")


def test_empty_node_selectors_falls_back_to_anywhere():
    cs = expand_candidates(_app([KubernetesCandidate(spec=K8sPlacementSpec(node_selectors=[]))]))
    assert len(cs) == 1 and cs[0].selector == {} and _selector_hash(cs[0].selector) == "none"


def test_overrides_resolved_onto_candidate():
    app = AppSpec(
        name="m",
        image="base:1",
        handler="h:H",
        env={"HF_HUB_OFFLINE": "1"},
        worker=WorkerSpec(
            type="sidecar", instances=3, args=["--flag"], server=ServerSpec(port=8080, request_path="/v1/segment")
        ),
        resources=ResourcesSpec(gpu=1, gpu_type="nvidia-l4"),
        placement=[
            KubernetesCandidate(provider="in-cluster", spec=K8sPlacementSpec(node_selectors=[{"gpu": "l4"}])),
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(machine_type="g2", regions=["r1"]),
                overrides=CandidateOverrides(instances=6, image="big:1", env={"SERVER__WORKERS": "6"}, args=["--xl"]),
            ),
        ],
    )
    cs = expand_candidates(app)
    k = cs[0]  # no overrides -> app-level worker config
    assert k.image == "base:1" and k.instances == 3 and k.worker_type == "sidecar"
    assert k.env["HF_HUB_OFFLINE"] == "1" and k.args == ["--flag"]
    assert k.server is not None and k.server.request_path == "/v1/segment"
    v = cs[1]  # per-candidate overrides win; app env still merged in
    assert v.image == "big:1" and v.instances == 6 and v.args == ["--xl"]
    assert v.env["HF_HUB_OFFLINE"] == "1" and v.env["SERVER__WORKERS"] == "6"
