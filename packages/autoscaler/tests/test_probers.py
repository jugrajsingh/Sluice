import datetime
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sluice_autoscaler.probers import Ec2StateProber, GceStateProber
from sluice_core.models import VmState


@pytest.fixture
def moto_server():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _ec2_tagspec(app: str, *, managed: bool = True) -> list[dict]:
    # moto's in-memory backend is process-global across the function-scoped server restarts, so each
    # test uses a DISTINCT app name — the sluice-app tag filter then isolates it from sibling tests.
    tags = [{"Key": "sluice-app", "Value": app}, {"Key": "sluice-pricing", "Value": "spot"}]
    if managed:
        tags.append({"Key": "sluice-managed", "Value": "true"})
    return [{"ResourceType": "instance", "Tags": tags}]


async def test_ec2_prober_maps_states(moto_server):
    import aioboto3

    session = aioboto3.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")
    async with session.client("ec2", endpoint_url=moto_server) as ec2:
        await ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=_ec2_tagspec("ec2map"),
        )
    prober = Ec2StateProber(region="us-east-1", endpoint_url=moto_server, access_key="t", secret_key="t")
    vms = await prober.instance_states("ec2map")
    assert len(vms) == 1 and vms[0].state is VmState.running and vms[0].pricing == "spot"


async def test_ec2_discovery_requires_managed_label(moto_server):
    import aioboto3

    session = aioboto3.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")
    async with session.client("ec2", endpoint_url=moto_server) as ec2:
        await ec2.run_instances(  # ours — has the managed label
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=_ec2_tagspec("ec2flt", managed=True),
        )
        await ec2.run_instances(  # NOT ours — sluice-app but no sluice-managed
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=_ec2_tagspec("ec2flt", managed=False),
        )
    prober = Ec2StateProber(region="us-east-1", endpoint_url=moto_server, access_key="t", secret_key="t")
    vms = await prober.instance_states("ec2flt")
    assert len(vms) == 1  # the unmanaged look-alike is NOT counted


async def test_ec2_delete_instance_terminates(moto_server):
    import aioboto3

    session = aioboto3.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")
    async with session.client("ec2", endpoint_url=moto_server) as ec2:
        r = await ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=_ec2_tagspec("ec2del"),
        )
        iid = r["Instances"][0]["InstanceId"]
    prober = Ec2StateProber(region="us-east-1", endpoint_url=moto_server, access_key="t", secret_key="t")
    await prober.delete_instance(iid)
    vms = await prober.instance_states("ec2del")
    assert len(vms) == 1 and vms[0].state is VmState.gone  # terminated


async def test_gce_prober_maps_states():
    payload = {
        "items": {
            "zones/r1-a": {
                "instances": [
                    {
                        "name": "sluice-m-abc",
                        "status": "RUNNING",
                        "machineType": ".../g2-standard-8",
                        "zone": ".../zones/r1-a",
                        "labels": {"sluice-app": "m", "sluice-pricing": "spot"},
                        "scheduling": {"preemptible": True},
                        "creationTimestamp": "2026-06-17T06:06:34-07:00",
                    },
                    {
                        "name": "sluice-m-def",
                        "status": "TERMINATED",
                        "machineType": ".../g2-standard-8",
                        "zone": ".../zones/r1-a",
                        "labels": {"sluice-app": "m", "sluice-pricing": "spot"},
                        "scheduling": {"preemptible": True},
                    },
                ]
            }
        }
    }

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(payload), content_type="application/json")

    app = web.Application()
    app.router.add_get("/compute/v1/projects/proj/aggregated/instances", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        prober = GceStateProber(
            project="proj", token_getter=lambda: "tok", api_root=str(server.make_url("")).rstrip("/")
        )
        vms = await prober.instance_states("m")
    finally:
        await server.close()
    states = {v.id: v.state for v in vms}
    assert states["sluice-m-abc"] is VmState.running
    assert states["sluice-m-def"] is VmState.preempted  # TERMINATED spot => preempted
    # created_at must come from the instance's real creationTimestamp (so the boot-deadline grace
    # works); a missing timestamp falls back to 0.0.
    created = {v.id: v.created_at for v in vms}
    assert created["sluice-m-abc"] == datetime.datetime.fromisoformat("2026-06-17T06:06:34-07:00").timestamp()
    assert created["sluice-m-def"] == 0.0
    # full zone is captured (needed to address the instance for delete/reset)
    assert {v.id: v.zone for v in vms}["sluice-m-abc"] == "r1-a"


async def test_gce_discovery_requires_managed_label():
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured["filter"] = request.query.get("filter", "")
        return web.Response(text=json.dumps({"items": {}}), content_type="application/json")

    app = web.Application()
    app.router.add_get("/compute/v1/projects/proj/aggregated/instances", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        prober = GceStateProber(
            project="proj", token_getter=lambda: "tok", api_root=str(server.make_url("")).rstrip("/")
        )
        await prober.instance_states("m")
    finally:
        await server.close()
    assert "labels.sluice-app=m" in captured["filter"]
    assert "labels.sluice-managed=true" in captured["filter"]


async def test_gce_delete_and_reset_address_the_zone_instance():
    seen: dict[str, dict] = {}

    async def delete_h(request: web.Request) -> web.Response:
        seen["delete"] = dict(request.match_info)
        return web.Response(text="{}", content_type="application/json")

    async def reset_h(request: web.Request) -> web.Response:
        seen["reset"] = dict(request.match_info)
        return web.Response(text="{}", content_type="application/json")

    app = web.Application()
    app.router.add_delete("/compute/v1/projects/proj/zones/{zone}/instances/{name}", delete_h)
    app.router.add_post("/compute/v1/projects/proj/zones/{zone}/instances/{name}/reset", reset_h)
    server = TestServer(app)
    await server.start_server()
    try:
        prober = GceStateProber(
            project="proj", token_getter=lambda: "tok", api_root=str(server.make_url("")).rstrip("/")
        )
        await prober.delete_instance("sluice-m-abc", "r1-a")
        await prober.reset_instance("sluice-m-abc", "r1-a")
    finally:
        await server.close()
    assert seen["delete"] == {"zone": "r1-a", "name": "sluice-m-abc"}
    assert seen["reset"] == {"zone": "r1-a", "name": "sluice-m-abc"}
