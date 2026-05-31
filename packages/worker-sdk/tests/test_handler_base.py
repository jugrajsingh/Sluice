from sluice_worker.handler import BaseHandler


async def test_default_health_true_after_load():
    class H(BaseHandler):
        async def predict(self, batch):
            return [b"x" for _ in batch]

    h = H()
    await h.load()
    assert await h.health() is True
