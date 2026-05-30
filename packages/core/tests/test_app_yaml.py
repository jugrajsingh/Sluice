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
    mode: both
    pricing: [spot, on-demand]
    kubernetes:
      nodePools:
        - { pricing: spot, selector: { cloud.google.com/gke-spot: "true" }, zones: [us-central1-a] }
    vm:
      provider: gce
      machineType: g2-standard-8
      acceleratorType: nvidia-l4
      regions: [us-central1, europe-west3]
      workersPerVm: 2
      lingerSeconds: 300
      maxVms: 5
"""


def test_parse_full_doc():
    app = parse_app_yaml(DOC)
    assert app.name == "topwear" and app.queue_ref == "topwear-q"
    assert app.storage_prefix == "custom/topwear"
    assert app.resources.gpu_type == "nvidia-l4" and app.resources.memory_gb == 8
    assert app.placement.mode == "both"
    assert app.placement.kubernetes[0].zones == ["us-central1-a"]
    assert app.placement.vm.machine_type == "g2-standard-8"
    assert app.placement.vm.workers_per_vm == 2


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
    ],
)
def test_rejects_bad_docs(bad):
    with pytest.raises(ValueError):
        parse_app_yaml(bad)
