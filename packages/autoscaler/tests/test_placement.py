from sluice_autoscaler.placement import candidate_key, expand_candidates
from sluice_core.models import (
    AppSpec,
    NodePoolSpec,
    PlacementSpec,
    ResourcesSpec,
    VmPlacementSpec,
)


def _app(mode="both", pricing=("spot", "on-demand")):
    return AppSpec(
        name="m",
        image="i",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="nvidia-l4"),
        placement=PlacementSpec(
            mode=mode,
            pricing=list(pricing),
            kubernetes=[
                NodePoolSpec(pricing="spot", selector={"gke-spot": "true"}, zones=["us-central1-a", "us-central1-c"]),
                NodePoolSpec(pricing="on-demand", selector={"pool": "od"}),
            ],
            vm=VmPlacementSpec(provider="gce", machine_type="g2-standard-8", regions=["us-central1", "europe-west3"]),
        ),
    )


def test_pricing_dominates_substrate():
    keys = [candidate_key(c) for c in expand_candidates(_app())]
    assert keys == [
        "kubernetes/k8s/us-central1-a/nvidia-l4/spot",
        "kubernetes/k8s/us-central1-c/nvidia-l4/spot",
        "vm/gce/us-central1/nvidia-l4/spot",
        "vm/gce/europe-west3/nvidia-l4/spot",
        "kubernetes/k8s/any/nvidia-l4/on-demand",
        "vm/gce/us-central1/nvidia-l4/on-demand",
        "vm/gce/europe-west3/nvidia-l4/on-demand",
    ]


def test_zone_pinned_selector_gets_topology_label():
    c = expand_candidates(_app())[0]
    assert c.selector == {"gke-spot": "true", "topology.kubernetes.io/zone": "us-central1-a"}


def test_mode_kubernetes_only():
    cs = expand_candidates(_app(mode="kubernetes"))
    assert all(c.substrate == "kubernetes" for c in cs)


def test_mode_vm_only():
    cs = expand_candidates(_app(mode="vm", pricing=("spot",)))
    assert [c.location for c in cs] == ["us-central1", "europe-west3"]
    assert all(c.substrate == "vm" for c in cs)
