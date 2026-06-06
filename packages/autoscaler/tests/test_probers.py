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


async def test_ec2_prober_maps_states(moto_server):
    import aioboto3

    session = aioboto3.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")
    async with session.client("ec2", endpoint_url=moto_server) as ec2:
        await ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "sluice-app", "Value": "m"}, {"Key": "sluice-pricing", "Value": "spot"}],
                }
            ],
        )
    prober = Ec2StateProber(region="us-east-1", endpoint_url=moto_server, access_key="t", secret_key="t")
    vms = await prober.instance_states("m")
    assert len(vms) == 1 and vms[0].state is VmState.running and vms[0].pricing == "spot"


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
