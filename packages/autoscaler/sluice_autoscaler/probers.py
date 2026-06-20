from __future__ import annotations

import datetime
from collections.abc import Callable

import aiohttp
from sluice_core.models import VmRecord, VmState


def _parse_ts(ts: str | None) -> float:
    """GCE creationTimestamp (RFC3339) -> epoch seconds; 0.0 if absent/unparseable."""
    if not ts:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


_EC2_STATE = {
    "pending": VmState.booting,
    "running": VmState.running,
    "stopping": VmState.stopped,
    "stopped": VmState.stopped,
    "shutting-down": VmState.gone,
    "terminated": VmState.gone,
}


class Ec2StateProber:
    def __init__(
        self,
        *,
        region: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import aioboto3

        self._session = aioboto3.Session(
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region
        )
        self._region = region
        self._kw = {"endpoint_url": endpoint_url} if endpoint_url else {}

    async def instance_states(self, app: str) -> list[VmRecord]:
        out: list[VmRecord] = []
        async with self._session.client("ec2", **self._kw) as ec2:
            # Require BOTH our app tag AND the managed tag (set by the TF module) so we never count/reap
            # an instance Sluice didn't create, even one that coincidentally carries a sluice-app tag.
            resp = await ec2.describe_instances(
                Filters=[
                    {"Name": "tag:sluice-app", "Values": [app]},
                    {"Name": "tag:sluice-managed", "Values": ["true"]},
                ]
            )
            for res in resp.get("Reservations", []):
                for inst in res.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    state = _EC2_STATE.get(inst["State"]["Name"], VmState.gone)
                    spot = inst.get("InstanceLifecycle") == "spot"
                    if spot and state == VmState.stopped:
                        state = VmState.preempted
                    out.append(
                        VmRecord(
                            id=inst["InstanceId"],
                            app=app,
                            provider="ec2",
                            region=self._region,
                            pricing=tags.get("sluice-pricing", "spot"),
                            machine_type=inst.get("InstanceType", ""),
                            state=state,
                        )
                    )
        return out

    async def delete_instance(self, name: str, zone: str = "") -> None:  # noqa: ARG002 (zone unused on EC2)
        """Terminate an instance by id (EC2 instance ids are region-addressable; `zone` is ignored)."""
        async with self._session.client("ec2", **self._kw) as ec2:
            await ec2.terminate_instances(InstanceIds=[name])

    async def reset_instance(self, name: str, zone: str = "") -> None:  # noqa: ARG002 (zone unused on EC2)
        """Reboot an instance by id (recovers a transient hang; `zone` is ignored on EC2)."""
        async with self._session.client("ec2", **self._kw) as ec2:
            await ec2.reboot_instances(InstanceIds=[name])


_GCE_STATE = {
    "PROVISIONING": VmState.booting,
    "STAGING": VmState.booting,
    "RUNNING": VmState.running,
    "STOPPING": VmState.stopped,
    "SUSPENDED": VmState.stopped,
    "TERMINATED": VmState.stopped,
}


class GceStateProber:
    def __init__(
        self, *, project: str, token_getter: Callable[[], str], api_root: str = "https://compute.googleapis.com"
    ) -> None:
        self._project = project
        self._token = token_getter
        self._root = api_root

    async def instance_states(self, app: str) -> list[VmRecord]:
        url = f"{self._root}/compute/v1/projects/{self._project}/aggregated/instances"
        # Require BOTH our app label AND the managed label (set by the TF module) so we never
        # count/reap an instance Sluice didn't create, even one that coincidentally carries sluice-app.
        params = {"filter": f"labels.sluice-app={app} AND labels.sluice-managed=true"}
        out: list[VmRecord] = []
        async with (
            aiohttp.ClientSession() as s,
            s.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"}) as resp,
        ):
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        for scope in (data.get("items") or {}).values():
            for inst in scope.get("instances", []) or []:
                labels = inst.get("labels", {})
                state = _GCE_STATE.get(inst.get("status", ""), VmState.gone)
                preemptible = inst.get("scheduling", {}).get("preemptible", False)
                if preemptible and inst.get("status") == "TERMINATED":
                    state = VmState.preempted
                zone = inst.get("zone", "").rsplit("/", 1)[-1]
                out.append(
                    VmRecord(
                        id=inst["name"],
                        app=app,
                        provider="gce",
                        region=zone.rsplit("-", 1)[0] if zone else "",
                        zone=zone,  # full zone, needed to address the instance for delete/reset
                        pricing=labels.get("sluice-pricing", "spot"),
                        machine_type=inst.get("machineType", "").rsplit("/", 1)[-1],
                        state=state,
                        # From the instance's real creation time so the boot-deadline grace works
                        # (without it, created_at defaults to 0.0 -> deadline in 1970 -> every booting
                        # VM looks dead immediately -> over-provision).
                        created_at=_parse_ts(inst.get("creationTimestamp")),
                    )
                )
        return out

    async def delete_instance(self, name: str, zone: str = "") -> None:
        """DELETE the instance (frees the disk; idempotent — a 404 means it is already gone)."""
        url = f"{self._root}/compute/v1/projects/{self._project}/zones/{zone}/instances/{name}"
        async with (
            aiohttp.ClientSession() as s,
            s.delete(url, headers={"Authorization": f"Bearer {self._token()}"}) as resp,
        ):
            if resp.status != 404:
                resp.raise_for_status()

    async def reset_instance(self, name: str, zone: str = "") -> None:
        """Hard-reboot the instance (recovers a transient hang; idempotent on a 404 = already gone)."""
        url = f"{self._root}/compute/v1/projects/{self._project}/zones/{zone}/instances/{name}/reset"
        async with (
            aiohttp.ClientSession() as s,
            s.post(url, headers={"Authorization": f"Bearer {self._token()}"}) as resp,
        ):
            if resp.status != 404:
                resp.raise_for_status()
