"""Terraform-backed ComputeProvider: one terraform state (and workdir) per VM."""

from __future__ import annotations

import asyncio
import json
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
        state_backend: dict[str, str],
        provider_defaults: dict[str, str],
        broker_url: str,
        signing_key: str,
        prober=None,
    ) -> None:
        self._tf = binary
        self._modules = Path(module_dir).resolve()
        self._root = Path(work_root)
        self._backend = state_backend
        self._defaults = provider_defaults
        self._broker_url = broker_url
        self._signing_key = signing_key
        self._prober = prober

    async def _run(self, workdir: Path, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self._tf, f"-chdir={workdir}", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(), err.decode()

    def _backend_tf(self, key: str) -> str:
        b = self._backend
        if b.get("type") == "s3":
            return (
                f'terraform {{\n  backend "s3" {{\n    bucket = {json.dumps(b["bucket"])}\n'
                f"    key = {json.dumps(key)}\n    region = {json.dumps(b.get('region', 'us-east-1'))}\n"
                f"  }}\n}}\n"
            )
        if b.get("type") == "gcs":
            return (
                f'terraform {{\n  backend "gcs" {{\n    bucket = {json.dumps(b["bucket"])}\n'
                f"    prefix = {json.dumps(key)}\n  }}\n}}\n"
            )
        return ""  # local state (dev)

    def _module_values(
        self, app: AppSpec, *, name: str, region: str, pricing: str, instances: int, worker_args, worker_type, server
    ) -> dict[str, object]:
        vc = _vm_candidate(app)
        provider, vm = vc.provider, vc.spec
        image = vc.overrides.image if vc.overrides and vc.overrides.image else app.image
        worker_env = {
            "WORKER__BROKER_URL": self._broker_url,
            "WORKER__BROKER_TOKEN": mint_worker_token(app=app.name, worker_id=name, key=self._signing_key),
            "WORKER__HANDLER": app.handler,
            "WORKER__APP": app.name,
            **app.env,
            **((vc.overrides.env if vc.overrides else None) or {}),
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
        # Hand the agent the full worker env explicitly (SLUICE_WORKER_ENV) so non-prefixed model env
        # (MODEL__*, HF_HUB_OFFLINE, ...) survives; plus the archetype/packing it must render.
        env = {
            **worker_env,
            "SLUICE_WORKER_ENV": json.dumps(worker_env),
            "SLUICE_WORKER_TYPE": worker_type,
            "SLUICE_INSTANCES": str(instances),
            "SLUICE_ARGS": json.dumps(worker_args),
        }
        common: dict[str, object] = {
            "name": name,
            "app": app.name,
            "spot": pricing == "spot",
            "worker_image": image,
            "workers_per_vm": instances,
            "linger_seconds": vm.linger_seconds,
            "env": env,
        }
        if provider == "gce":
            common |= {
                "zone": region + self._defaults.get("zone_suffix", "-a"),
                "machine_type": vm.machine_type,
                "accelerator_type": vm.accelerator_type,
                "project": self._defaults.get("project", ""),
            }
            if vm.boot_image:
                common["boot_image"] = vm.boot_image
        else:  # ec2
            common |= {
                "instance_type": vm.machine_type,
                "ami": vm.boot_image,
                "iam_instance_profile": self._defaults.get("iam_instance_profile", ""),
            }
        return common

    def _render(self, app: AppSpec, *, name: str, region: str, pricing: str, **worker_kw) -> Path:
        provider = _vm_candidate(app).provider
        workdir = self._root / app.name / region / name
        workdir.mkdir(parents=True, exist_ok=True)
        values = self._module_values(app, name=name, region=region, pricing=pricing, **worker_kw)
        lines = ['module "vm" {', f"  source = {json.dumps(str(self._modules / f'sluice-vm-{provider}'))}"]
        lines += [f"  {k} = {_hcl(v)}" for k, v in values.items()]
        lines += ["}", 'output "instance_name" { value = module.vm.instance_name }']
        (workdir / "main.tf").write_text("\n".join(lines) + "\n")
        (workdir / "backend.tf").write_text(self._backend_tf(f"sluice/apps/{app.name}/tf/{region}/{name}"))
        return workdir

    async def provision(
        self,
        app: AppSpec,
        *,
        region: str,
        pricing: str,
        count: int,
        instances: int = 1,
        args: list[str] | None = None,
        worker_type: str = "handler",
        server=None,
    ) -> list[VmRecord]:
        vc = _vm_candidate(app)
        provider, vm = vc.provider, vc.spec
        worker_kw = {
            "instances": instances,
            "worker_args": list(args or []),
            "worker_type": worker_type,
            "server": server,
        }
        records: list[VmRecord] = []
        for _ in range(count):
            name = f"sluice-{app.name}-{uuid.uuid4().hex[:6]}"
            workdir = self._render(app, name=name, region=region, pricing=pricing, **worker_kw)
            for args in (
                ("init", "-input=false"),
                ("plan", "-out=plan.tfplan", "-input=false"),
                ("apply", "plan.tfplan"),
            ):
                rc, _out, err = await self._run(workdir, *args)
                if rc != 0:
                    await self._run(workdir, "destroy", "-auto-approve", "-input=false")
                    raise ProvisionFailure(classify_error(err), err.strip()[-500:])
            rc, out, _err = await self._run(workdir, "output", "-json")
            instance = name
            if rc == 0 and out.strip():
                instance = json.loads(out).get("instance_name", {}).get("value", name)
            if instance != name:
                workdir.rename(workdir.with_name(instance))
            records.append(
                VmRecord(
                    id=instance,
                    app=app.name,
                    provider=provider,
                    region=region,
                    pricing=pricing,
                    machine_type=vm.machine_type,
                    state=VmState.provisioning,
                    created_at=time.time(),
                )
            )
        return records

    async def instance_states(self, app: str) -> list[VmRecord]:
        if self._prober is None:
            return []
        return await self._prober.instance_states(app)

    async def destroy(self, app: str, vm_ids: list[str]) -> None:
        if not (self._root / app).exists():
            return
        for main_tf in (self._root / app).rglob("main.tf"):
            if main_tf.parent.name in vm_ids:
                await self._run(main_tf.parent, "destroy", "-auto-approve", "-input=false")
