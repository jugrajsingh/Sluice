import json
import stat

import pytest
from sluice_autoscaler.terraform import TerraformProvider, classify_error
from sluice_core.errors import ProvisionFailure
from sluice_core.models import (
    AppSpec,
    BatchSpec,
    ProvisionError,
    ResourcesSpec,
    VmCandidate,
    VmPlacementSpec,
    VmRecord,
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
        placement=[
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(
                    pricing="spot",
                    machine_type="g2-standard-8",
                    accelerator_type="nvidia-l4",
                    regions=["r1"],
                ),
            )
        ],
    )


def _provider(binary, tmp_path):
    return TerraformProvider(
        binary=binary,
        module_dir="infra/terraform/modules",
        work_root=str(tmp_path / "work"),
        provider_defaults={"project": "proj", "zone_suffix": "-a"},
        broker_url="http://sluice-gateway",
        signing_key="tf-signing-key",  # gitleaks:allow (test fixture, not a secret)
        keep_workdir=True,  # retain the rendered workdir so these tests can inspect main.tf
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
    assert "SLUICE_WORKER_ENV" in text  # full worker env handed to the VM agent verbatim
    assert not (workdirs[0].parent / "backend.tf").exists()  # LOCAL state — no remote backend (ADR-012)


async def test_provision_stockout_classifies_and_destroys(fake_terraform, tmp_path, monkeypatch):
    binary, calls = fake_terraform
    monkeypatch.setenv("TF_FAKE_MODE", "stockout")
    with pytest.raises(ProvisionFailure) as e:
        await _provider(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    assert e.value.kind is ProvisionError.STOCKOUT
    assert any("destroy" in c for c in calls())  # cleanup attempted


async def test_provision_discards_workdir_on_success(fake_terraform, tmp_path):
    # Ephemeral local state (ADR-012): a successful provision leaves NO workdir behind (the cloud holds
    # the VM; the prober is the source of truth). The GCE zone is set so a later reap can address it.
    binary, _calls = fake_terraform
    p = TerraformProvider(
        binary=binary,
        module_dir="infra/terraform/modules",
        work_root=str(tmp_path / "work"),
        provider_defaults={"project": "proj", "zone_suffix": "-a"},
        broker_url="http://sluice-gateway",
        signing_key="tf-signing-key",  # gitleaks:allow (test fixture, not a secret)
    )
    vms = await p.provision(_app(), region="r1", pricing="spot", count=1)
    assert len(vms) == 1 and vms[0].zone == "r1-a"
    assert list((tmp_path / "work").rglob("main.tf")) == []  # workdir discarded


class _FakeProber:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.reset: list[tuple[str, str]] = []

    async def instance_states(self, app: str) -> list[VmRecord]:
        return []

    async def delete_instance(self, name: str, zone: str = "") -> None:
        self.deleted.append((name, zone))

    async def reset_instance(self, name: str, zone: str = "") -> None:
        self.reset.append((name, zone))


async def test_delete_and_reset_instance_delegate_to_prober(fake_terraform, tmp_path):
    binary, _calls = fake_terraform
    prober = _FakeProber()
    p = TerraformProvider(
        binary=binary,
        module_dir="infra/terraform/modules",
        work_root=str(tmp_path / "work"),
        provider_defaults={"project": "proj", "zone_suffix": "-a"},
        broker_url="http://sluice-gateway",
        signing_key="tf-signing-key",  # gitleaks:allow (test fixture, not a secret)
        prober=prober,
    )
    rec = VmRecord(
        id="sluice-m-abc", app="m", provider="gce", region="r1", zone="r1-a", pricing="spot", machine_type="g2"
    )
    await p.delete_instance(rec)
    await p.reset_instance(rec)
    assert prober.deleted == [("sluice-m-abc", "r1-a")]  # reaped by name+zone (no terraform destroy)
    assert prober.reset == [("sluice-m-abc", "r1-a")]


async def test_selected_candidate_drives_render_not_first_vm_candidate(fake_terraform, tmp_path):
    # the app's first vm candidate is gce/g2-standard-8; provisioning a DIFFERENT selected candidate
    # (ec2/g5.xlarge, instances 4) must render THAT, not the first one (heterogeneous-GPU bug fix).
    from sluice_autoscaler.placement import Candidate

    binary, _calls = fake_terraform
    cand = Candidate(
        type="vm",
        pricing="on-demand",
        cluster="ec2",
        location="us-east-1",
        machine_type="g5.xlarge",
        boot_image="ami-123",
        instances=4,
        image="my/override:1",
        env={"MODEL__VARIANT": "sam3.1"},
    )
    await _provider(binary, tmp_path).provision(
        _app(), region="us-east-1", pricing="on-demand", count=1, candidate=cand, instances=4
    )
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    assert "sluice-vm-ec2" in text and '"g5.xlarge"' in text  # the selected candidate's cloud + machine
    assert '"g2-standard-8"' not in text  # NOT the app's first vm candidate
    assert "SLUICE_INSTANCES" in text and '"4"' in text and "my/override:1" in text


def _provider_with_base(binary, tmp_path):
    return TerraformProvider(
        binary=binary,
        module_dir="infra/terraform/modules",
        work_root=str(tmp_path / "work"),
        provider_defaults={"project": "proj", "zone_suffix": "-a"},
        broker_url="http://sluice-gateway",
        signing_key="tf-signing-key",  # gitleaks:allow (test fixture, not a secret)
        worker_base_image="gcr.io/x/sluice-worker-base:test",
        keep_workdir=True,  # retain the rendered workdir so these tests can inspect main.tf
    )


async def test_two_image_vm_render_carries_worker_base_and_app_image(fake_terraform, tmp_path):
    # The VM agent (+ sidecar adapter) run in the worker-base image; the BYO model image rides as
    # APP_IMAGE for the server/launcher. A GPU app sets SLUICE_GPU=1 (-> docker run --gpus all).
    binary, _calls = fake_terraform
    await _provider_with_base(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    assert "gcr.io/x/sluice-worker-base:test" in text  # worker_image = the worker-base
    assert "APP_IMAGE" in text and "repo/worker:1" in text  # the model image handed to the agent
    assert "SLUICE_GPU" in text and '"1"' in text  # GPU app


async def test_cpu_app_renders_gpu_off(fake_terraform, tmp_path):
    binary, _calls = fake_terraform
    cpu_app = AppSpec(
        name="c",
        image="repo/cpu:1",
        handler="h:H",
        resources=ResourcesSpec(gpu=0),
        placement=[
            VmCandidate(
                provider="gce",
                spec=VmPlacementSpec(pricing="spot", machine_type="e2-standard-4", regions=["r1"]),
            )
        ],
    )
    await _provider_with_base(binary, tmp_path).provision(cpu_app, region="r1", pricing="spot", count=1)
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    assert "SLUICE_GPU" in text and '"0"' in text  # CPU app -> no --gpus on the VM


async def test_should_not_inject_object_store_creds_into_vm_when_provisioning(fake_terraform, tmp_path):
    # The VM holds NO backend object-store credentials (ADR-002/008): the agent and workers reach the
    # store only through the gateway broker (presigned URLs / proxied small JSON), so a GCE VM works
    # against an S3 store and vice-versa. The agent's broker channel rides the short-lived JWT.
    binary, _calls = fake_terraform
    await _provider_with_base(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    assert "OBJECT_STORE__" not in text  # no backend store creds anywhere on the VM
    assert "WORKER__BROKER_URL" in text  # the broker channel (URL + JWT) is what reaches the VM


def _batch_app():
    app = _app()
    app.worker.type = "sidecar"
    app.batch = BatchSpec(batchSlaHours=24, outputPartitionSize=500, starveGraceMin=5)
    return app


def _worker_env_from_render(text: str) -> dict[str, str]:
    """Extract the SLUICE_WORKER_ENV JSON the agent forwards to the worker/adapter containers.

    ``env`` is rendered as a single-line HCL map; the ``SLUICE_WORKER_ENV`` value is a
    json.dumps()'d dict, itself json.dumps()'d again by ``_hcl`` into a quoted HCL string.
    Recover it by scanning to the matching close-quote of that escaped JSON string.
    """
    marker = '"SLUICE_WORKER_ENV" = "'
    start = text.index(marker) + len(marker) - 1  # include the opening quote
    rest = text[start:]
    decoder = json.JSONDecoder()
    inner, _ = decoder.raw_decode(rest)  # the json.dumps'd env dict (still a JSON string)
    return json.loads(inner)


async def test_should_enable_batch_lane_env_when_app_has_batch_block(fake_terraform, tmp_path):
    binary, _calls = fake_terraform
    app = _batch_app()
    await _provider_with_base(binary, tmp_path).provision(
        app, region="r1", pricing="spot", count=1, worker_type="sidecar", server=app.worker.server
    )
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    wenv = _worker_env_from_render(text)
    # the adapter container learns it must run the batch lane
    assert wenv["WORKER__BATCH_ENABLED"] == "1"
    assert wenv["WORKER__BATCH_OUTPUT_PARTITION_SIZE"] == "500"
    assert wenv["WORKER__APP"] == "m"
    # and it holds ONLY its JWT — no backend object-store creds (output/status go through the broker)
    assert not any(k.startswith("OBJECT_STORE__") for k in wenv)


async def test_should_not_set_batch_env_when_app_has_no_batch_block(fake_terraform, tmp_path):
    binary, _calls = fake_terraform
    await _provider_with_base(binary, tmp_path).provision(_app(), region="r1", pricing="spot", count=1)
    text = next((tmp_path / "work").rglob("main.tf")).read_text()
    wenv = _worker_env_from_render(text)
    assert "WORKER__BATCH_ENABLED" not in wenv


def test_classify_error_table():
    assert classify_error("ZONE_RESOURCE_POOL_EXHAUSTED") is ProvisionError.STOCKOUT
    assert classify_error("InsufficientInstanceCapacity") is ProvisionError.STOCKOUT
    assert classify_error("Quota 'NVIDIA_L4_GPUS' exceeded") is ProvisionError.QUOTA
    assert classify_error("403 AccessDenied") is ProvisionError.AUTH
    assert classify_error("something else") is ProvisionError.OTHER
