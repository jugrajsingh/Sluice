from __future__ import annotations

from collections.abc import Callable

import aiohttp
from sluice_core.models import VmRecord, VmState

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
            resp = await ec2.describe_instances(Filters=[{"Name": "tag:sluice-app", "Values": [app]}])
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
        params = {"filter": f"labels.sluice-app={app}"}
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
                        pricing=labels.get("sluice-pricing", "spot"),
                        machine_type=inst.get("machineType", "").rsplit("/", 1)[-1],
                        state=state,
                    )
                )
        return out
