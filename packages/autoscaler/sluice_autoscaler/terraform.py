"""Terraform-backed ComputeProvider: one terraform state (and workdir) per VM."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

from sluice_core.auth import mint_worker_token
from sluice_core.errors import ProvisionFailure
from sluice_core.models import AppSpec, ProvisionError, VmRecord, VmState

_STOCKOUT = (
    "ZONE_RESOURCE_POOL_EXHAUSTED",
    "InsufficientInstanceCapacity",
    "resource pool exhausted",
    "does not have enough resources",
)
_QUOTA = ("Quota", "QUOTA", "quota")
_AUTH = ("403", "401", "AccessDenied", "credentials", "Unauthorized")


def _vm_candidate(app: AppSpec):
    """The app's burst-VM candidate (provider + spec); None if the app has no vm placement."""
    return next((c for c in app.placement if c.type == "vm"), None)


def classify_error(stderr: str) -> ProvisionError:
    if any(p in stderr for p in _STOCKOUT):
        return ProvisionError.STOCKOUT
    if any(p in stderr for p in _QUOTA):
        return ProvisionError.QUOTA
    if any(p in stderr for p in _AUTH):
        return ProvisionError.AUTH
    return ProvisionError.OTHER


def _hcl(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{json.dumps(k)} = {_hcl(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    return json.dumps(value)


class TerraformProvider:
    def __init__(
        self,
        *,
        binary: str = "terraform",
        module_dir: str,
        work_root: str,
        provider_defaults: dict[str, str],
        broker_url: str,
        signing_key: str,
        worker_base_image: str = "",
        prober=None,
        keep_workdir: bool = False,
    ) -> None:
        self._tf = binary
        self._modules = Path(module_dir).resolve()
        self._root = Path(work_root)
        self._defaults = provider_defaults
        self._broker_url = broker_url
        self._signing_key = signing_key
        # The Sluice worker-base image runs the VM agent + (sidecar) adapter; the app image runs the
        # model server / handler launcher (passed to the agent as APP_IMAGE). Empty ⇒ the app image
        # serves both (a combined image must then bundle sluice_worker).
        self._worker_base_image = worker_base_image
        # Stateless lifecycle (ADR-012): `provision` CREATEs an immutable VM in an ephemeral LOCAL-state
        # workdir, then discards it (the cloud holds the VM; the prober is the source of truth). DELETE is
        # a direct cloud-API call (delete_instance/reset_instance) via the prober — never terraform destroy.
        self._prober = prober
        self._keep_workdir = keep_workdir  # retain workdirs for debugging instead of discarding on success

    async def _run(self, workdir: Path, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self._tf, f"-chdir={workdir}", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(), err.decode()

    def _module_values(
        self,
        app: AppSpec,
        *,
        name: str,
        region: str,
        pricing: str,
        provider: str,
        machine_type: str,
        accelerator_type: str,
        boot_image: str,
        linger_seconds: int,
        image: str,
        env_extra: dict[str, str],
        instances: int,
        worker_args,
        worker_type,
        server,
    ) -> dict[str, object]:
        worker_env = {
            "WORKER__BROKER_URL": self._broker_url,
            "WORKER__BROKER_TOKEN": mint_worker_token(app=app.name, worker_id=name, key=self._signing_key),
            "WORKER__HANDLER": app.handler,
            "WORKER__APP": app.name,
            **env_extra,
        }
        if worker_type == "sidecar" and server is not None:
            worker_env |= {
                "WORKER__CONCURRENCY": str(server.concurrency or instances),
                "WORKER__SERVER_PORT": str(server.port),
                "WORKER__SERVER_REQUEST_PATH": server.request_path,
                "WORKER__SERVER_METHOD": server.method,
                "WORKER__SERVER_CONTENT_TYPE": server.content_type,
                "WORKER__SERVER_HEALTH_PATH": server.health_path,
                "WORKER__SERVER_READY_TIMEOUT_S": str(server.ready_timeout_s),
            }
        if app.batch is not None:
            # An app with a batch block runs the dual-source batch lane. The adapter leases
            # {app}-batch files through the SAME broker JWT (already in worker_env), fetches input via
            # the presigned body_url, writes output via broker-minted presigned PUTs, and proxies
            # status through the broker — so it needs NO object-store credentials (ADR-002/008). Only
            # the batch knobs are added here.
            worker_env |= {
                "WORKER__BATCH_ENABLED": "1",
                "WORKER__BATCH_OUTPUT_PARTITION_SIZE": str(app.batch.output_partition_size),
                "WORKER__PUT_CONCURRENCY": str(app.scaling.put_concurrency),
                "WORKER__STARVE_GRACE_S": str(app.batch.starve_grace_min * 60),
            }
        # Hand the agent the full worker env explicitly (SLUICE_WORKER_ENV) so non-prefixed model env
        # (MODEL__*, HF_HUB_OFFLINE, ...) survives; plus the archetype/packing it must render.
        env = {
            **worker_env,
            "SLUICE_WORKER_ENV": json.dumps(worker_env),
            "SLUICE_WORKER_TYPE": worker_type,
            "SLUICE_INSTANCES": str(instances),
            "SLUICE_ARGS": json.dumps(worker_args),
            # The model image (sidecar server / handler launcher) the agent runs; and whether the
            # unit gets a GPU (`docker run --gpus all`). app.resources.gpu == 0 ⇒ CPU-only VM.
            "APP_IMAGE": image,
            "SLUICE_GPU": "1" if app.resources.gpu else "0",
            # The VM agent reports heartbeats / receives commands through the gateway broker using the
            # same short-lived JWT the workers hold (WORKER__BROKER_URL/TOKEN, spread in from worker_env
            # above) — NO object-store credentials are placed on the VM (ADR-002/008), so a GCE VM works
            # against an S3 store and vice-versa.
        }
        common: dict[str, object] = {
            "name": name,
            "app": app.name,
            "spot": pricing == "spot",
            # the VM agent (and sidecar adapter) run in the worker-base; the app image rides in APP_IMAGE
            "worker_image": self._worker_base_image or image,
            "workers_per_vm": instances,
            "linger_seconds": linger_seconds,
            "env": env,
        }
        if provider == "gce":
            common |= {
                "zone": region + self._defaults.get("zone_suffix", "-a"),
                "machine_type": machine_type,
                "accelerator_type": accelerator_type,
                "project": self._defaults.get("project", ""),
                # the SA attached to the VM: gives it ambient ADC to pull private images (gcr.io) and
                # the model weights (gs://) via the metadata server — no mounted worker creds.
                "service_account_email": self._defaults.get("service_account_email", ""),
            }
            if boot_image:
                common["boot_image"] = boot_image
        else:  # ec2
            common |= {
                "instance_type": machine_type,
                "ami": boot_image,
                "iam_instance_profile": self._defaults.get("iam_instance_profile", ""),
            }
        return common

    def _vm_view(self, app: AppSpec, *, candidate, instances, args, worker_type, server) -> dict:
        """Resolve the vm render inputs from the *selected* candidate, falling back to the app's
        single vm candidate for direct callers (so a heterogeneous app renders the right machine)."""
        if candidate is not None:
            return {
                "provider": candidate.cluster,
                "machine_type": candidate.machine_type,
                "accelerator_type": candidate.accelerator_type,
                "boot_image": candidate.boot_image,
                "linger_seconds": candidate.linger_seconds,
                "image": candidate.image,
                "env_extra": candidate.env,
                "instances": candidate.instances,
                "worker_args": candidate.args,
                "worker_type": candidate.worker_type,
                "server": candidate.server,
            }
        vc = _vm_candidate(app)
        ov = vc.overrides
        return {
            "provider": vc.provider,
            "machine_type": vc.spec.machine_type,
            "accelerator_type": vc.spec.accelerator_type,
            "boot_image": vc.spec.boot_image,
            "linger_seconds": vc.spec.linger_seconds,
            "image": (ov.image if ov and ov.image else app.image),
            "env_extra": {**app.env, **((ov.env if ov else None) or {})},
            "instances": instances,
            "worker_args": list(args or []),
            "worker_type": worker_type,
            "server": server,
        }

    def _render(self, app: AppSpec, *, name: str, region: str, pricing: str, vm_view: dict) -> Path:
        workdir = self._root / app.name / region / name
        workdir.mkdir(parents=True, exist_ok=True)
        values = self._module_values(app, name=name, region=region, pricing=pricing, **vm_view)
        lines = ['module "vm" {', f"  source = {json.dumps(str(self._modules / f'sluice-vm-{vm_view["provider"]}'))}"]
        lines += [f"  {k} = {_hcl(v)}" for k, v in values.items()]
        lines += ["}", 'output "instance_name" { value = module.vm.instance_name }']
        # LOCAL state only — no backend.tf. The workdir is ephemeral (discarded after a successful apply
        # unless keep_workdir); the cloud holds the VM and the prober is the source of truth (ADR-012).
        (workdir / "main.tf").write_text("\n".join(lines) + "\n")
        return workdir

    async def provision(
        self,
        app: AppSpec,
        *,
        region: str,
        pricing: str,
        count: int,
        candidate=None,
        instances: int = 1,
        args: list[str] | None = None,
        worker_type: str = "handler",
        server=None,
    ) -> list[VmRecord]:
        vm_view = self._vm_view(
            app, candidate=candidate, instances=instances, args=args, worker_type=worker_type, server=server
        )
        records: list[VmRecord] = []
        for _ in range(count):
            name = f"sluice-{app.name}-{uuid.uuid4().hex[:6]}"
            workdir = self._render(app, name=name, region=region, pricing=pricing, vm_view=vm_view)
            try:
                for tf_args in (
                    ("init", "-input=false"),
                    ("plan", "-out=plan.tfplan", "-input=false"),
                    ("apply", "plan.tfplan"),
                ):
                    rc, _out, err = await self._run(workdir, *tf_args)
                    if rc != 0:
                        # Clean any partially-created resource via the LOCAL state before surfacing the error.
                        await self._run(workdir, "destroy", "-auto-approve", "-input=false")
                        # Keep the tail of stderr where terraform prints the Error block (the `Error:`
                        # line + resource + troubleshooting footer). 500 chars caught only the footer.
                        raise ProvisionFailure(classify_error(err), err.strip()[-2000:])
                rc, out, _err = await self._run(workdir, "output", "-json")
            finally:
                self._discard(workdir)  # ephemeral state — never reused (the prober is the truth)
            instance = name
            if rc == 0 and out.strip():
                instance = json.loads(out).get("instance_name", {}).get("value", name)
            records.append(
                VmRecord(
                    id=instance,
                    app=app.name,
                    provider=vm_view["provider"],
                    region=region,
                    # GCE zone (region+suffix) so a later reap can address the instance; EC2 is region-bound.
                    zone=(region + self._defaults.get("zone_suffix", "-a")) if vm_view["provider"] == "gce" else "",
                    pricing=pricing,
                    machine_type=vm_view["machine_type"],
                    state=VmState.provisioning,
                    created_at=time.time(),
                )
            )
        return records

    def _discard(self, workdir: Path) -> None:
        """Remove the ephemeral local-state workdir after apply (kept only when keep_workdir is set)."""
        if not self._keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    async def instance_states(self, app: str) -> list[VmRecord]:
        if self._prober is None:
            return []
        return await self._prober.instance_states(app)

    async def delete_instance(self, record: VmRecord) -> None:
        """Reap a VM by a direct cloud-API delete (never terraform destroy). No-op without a prober."""
        if self._prober is not None:
            await self._prober.delete_instance(record.id, record.zone)

    async def reset_instance(self, record: VmRecord) -> None:
        """Hard-reboot a (hung) VM via the cloud API to try to recover it. No-op without a prober."""
        if self._prober is not None:
            await self._prober.reset_instance(record.id, record.zone)
