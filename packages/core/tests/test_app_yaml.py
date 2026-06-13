import pytest
from sluice_core.app_yaml import parse_app_yaml, serialize_app_yaml

DOC = """
apiVersion: sluice/v1
kind: App
metadata: { name: topwear }
spec:
  image: ghcr.io/acme/topwear:1.2.0
  handler: handler:SegHandler
  queue: { ref: topwear-q }
  storage: { prefix: custom/topwear }
  resources: { gpu: 1, gpuType: nvidia-l4, cpu: 2, memoryGb: 8 }
  scaling: { messagesPerWorker: 10, maxWorkers: 0, scaleUpCount: 3, cooldownSeconds: 30, scheduleGraceSeconds: 180 }
  placement:
    - type: kubernetes
      provider: in-cluster
      spec:
        pricing: spot
        nodeSelectors:
          - { cloud.google.com/gke-nodepool: l4-spot, gpu: l4, lifecycle: spot }
          - { cloud.google.com/gke-spot: "true" }
        tolerations:
          - { key: nvidia.com/gpu, operator: Exists, effect: NoSchedule }
        scheduleGraceSeconds: 120
    - type: vm
      provider: gce
      spec:
        pricing: spot
        machineType: g2-standard-8
        acceleratorType: nvidia-l4
        regions: [us-central1, europe-west3]
        workersPerVm: 2
        lingerSeconds: 300
        maxVms: 5
    - type: kubernetes
      provider: gke-east
      spec:
        pricing: on-demand
        nodeSelectors:
          - { cloud.google.com/gke-nodepool: l4-ondemand, gpu: l4 }
"""


def test_parse_full_doc():
    app = parse_app_yaml(DOC)
    assert app.name == "topwear" and app.queue_ref == "topwear-q"
    assert app.storage_prefix == "custom/topwear"
    assert app.resources.gpu_type == "nvidia-l4" and app.resources.memory_gb == 8
    # placement is an ordered list of typed candidates
    assert [c.type for c in app.placement] == ["kubernetes", "vm", "kubernetes"]
    k0 = app.placement[0]
    assert k0.provider == "in-cluster" and k0.spec.pricing == "spot"
    assert k0.spec.node_selectors[0] == {"cloud.google.com/gke-nodepool": "l4-spot", "gpu": "l4", "lifecycle": "spot"}
    assert k0.spec.tolerations[0].key == "nvidia.com/gpu"
    assert k0.spec.schedule_grace_s == 120
    vm = app.placement[1]
    assert vm.provider == "gce" and vm.spec.machine_type == "g2-standard-8" and vm.spec.workers_per_vm == 2
    assert app.placement[2].provider == "gke-east" and app.placement[2].spec.pricing == "on-demand"


def test_ordered_node_selectors_preserved():
    app = parse_app_yaml(DOC)
    selectors = app.placement[0].spec.node_selectors
    assert len(selectors) == 2
    assert "cloud.google.com/gke-nodepool" in selectors[0]  # targeted pool first
    assert selectors[1] == {"cloud.google.com/gke-spot": "true"}  # broader fallback second


def test_roundtrip():
    app = parse_app_yaml(DOC)
    again = parse_app_yaml(serialize_app_yaml(app))
    assert again == app


@pytest.mark.parametrize(
    "bad",
    [
        "apiVersion: nope/v1\nkind: App\nmetadata: {name: x}\nspec: {image: i}",
        "apiVersion: sluice/v1\nkind: Pod\nmetadata: {name: x}\nspec: {image: i}",
        "apiVersion: sluice/v1\nkind: App\nmetadata: {}\nspec: {image: i}",
        # unknown placement candidate type is rejected by the discriminated union
        "apiVersion: sluice/v1\nkind: App\nmetadata: {name: x}\n"
        "spec:\n  image: i\n  placement:\n    - {type: serverless}",
    ],
)
def test_rejects_bad_docs(bad):
    with pytest.raises(ValueError):
        parse_app_yaml(bad)
