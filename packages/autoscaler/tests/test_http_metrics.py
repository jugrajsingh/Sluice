from aiohttp.test_utils import TestClient, TestServer
from sluice_autoscaler.http import build_health_app
from sluice_autoscaler.metrics import REGISTRY, SCALE_UP_PODS


async def test_healthz_and_metrics_served():
    async with TestClient(TestServer(build_health_app())) as c:
        r = await c.get("/healthz")
        assert r.status == 200 and (await r.text()) == "ok"
        m = await c.get("/metrics")
        assert m.status == 200
        assert "sluice_reconcile_seconds" in (await m.text())


def test_counters_register():
    SCALE_UP_PODS.labels(app="t").inc(2)
    assert REGISTRY.get_sample_value("sluice_scale_up_pods_total", {"app": "t"}) == 2.0
