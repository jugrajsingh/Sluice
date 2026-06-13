import stat

import pytest
from sluice_autoscaler.terraform import TerraformProvider, classify_error
from sluice_core.errors import ProvisionFailure
from sluice_core.models import (
    AppSpec,
    PlacementSpec,
    ProvisionError,
    ResourcesSpec,
    VmPlacementSpec,
)

FAKE_TF = """#!/bin/bash
echo "$@" >> "$TF_FAKE_LOG"
case "$TF_FAKE_MODE" in
  stockout)
    if [[ "$*" == *apply* ]]; then
      echo "Error: creating instance: ZONE_RESOURCE_POOL_EXHAUSTED ..." >&2
      exit 1
    fi ;;
esac
if [[ "$*" == *"output"* ]]; then
  echo '{"instance_name": {"value": "'"$TF_FAKE_NAME"'"}}'
fi
exit 0
"""


@pytest.fixture
def fake_terraform(tmp_path, monkeypatch):
    binary = tmp_path / "terraform"
    binary.write_text(FAKE_TF)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "tf.log"
    log.touch()
    monkeypatch.setenv("TF_FAKE_LOG", str(log))
    monkeypatch.setenv("TF_FAKE_MODE", "ok")
    monkeypatch.setenv("TF_FAKE_NAME", "sluice-m-abc123")

    def calls() -> list[str]:
        return [line for line in log.read_text().splitlines() if line]

    return str(binary), calls


def _app():
    return AppSpec(
        name="m",
        image="repo/worker:1",
        handler="h:H",
        resources=ResourcesSpec(gpu=1, gpu_type="nvidia-l4"),
        placement=PlacementSpec(
            mode="vm",
            vm=VmPlacementSpec(
                provider="gce",
                machine_type="g2-standard-8",
                accelerator_type="nvidia-l4",
                regions=["r1"],
                workers_per_vm=2,
            ),
        ),
    )


def _provider(binary, tmp_path):
    return TerraformProvider(
        binary=binary,
        module_dir="infra/terraform/modules",
        work_root=str(tmp_path / "work"),
        state_backend={"type": "s3", "bucket": "b", "region": "us-east-1"},
        provider_defaults={"project": "proj", "zone_suffix": "-a"},
        broker_url="http://sluice-gateway",
        signing_key="tf-signing-key",  # gitleaks:allow (test fixture, not a secret)
    )


async def test_provision_renders_plans_applies(fake_terraform, tmp_path):
    binary, calls = fake_terraform
    vms = await _provider(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    assert len(vms) == 1 and vms[0].id == "sluice-m-abc123" and vms[0].region == "r1"
    joined = "\n".join(calls())
    assert "init" in joined and "plan -out=plan.tfplan" in joined and "apply plan.tfplan" in joined
    # rendered main.tf carries the spec values
    workdirs = list((tmp_path / "work").rglob("main.tf"))
    text = workdirs[0].read_text()
    assert '"g2-standard-8"' in text and "sluice-vm-gce" in text and "spot = true" in text
    assert 'backend "s3"' in (workdirs[0].parent / "backend.tf").read_text()


async def test_provision_stockout_classifies_and_destroys(fake_terraform, tmp_path, monkeypatch):
    binary, calls = fake_terraform
    monkeypatch.setenv("TF_FAKE_MODE", "stockout")
    with pytest.raises(ProvisionFailure) as e:
        await _provider(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    assert e.value.kind is ProvisionError.STOCKOUT
    assert any("destroy" in c for c in calls())  # cleanup attempted


async def test_destroy_runs_in_vm_workdir(fake_terraform, tmp_path):
    binary, calls = fake_terraform
    p = _provider(binary, tmp_path)
    vms = await p.provision(_app(), region="r1", pricing="spot", count=1)
    await p.destroy("m", [vms[0].id])
    assert any("destroy -auto-approve" in c for c in calls())


def test_classify_error_table():
    assert classify_error("ZONE_RESOURCE_POOL_EXHAUSTED") is ProvisionError.STOCKOUT
    assert classify_error("InsufficientInstanceCapacity") is ProvisionError.STOCKOUT
    assert classify_error("Quota 'NVIDIA_L4_GPUS' exceeded") is ProvisionError.QUOTA
    assert classify_error("403 AccessDenied") is ProvisionError.AUTH
    assert classify_error("something else") is ProvisionError.OTHER
