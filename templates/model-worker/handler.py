"""Copy me. Implement load() + predict(); point WORKER__HANDLER at this class."""

from sluice_worker.handler import BaseHandler


class MyHandler(BaseHandler):
    async def load(self) -> None:
        await super().load()
        # self.model = load_your_model("/mnt/models/...")  # runs once on pod start

    async def predict(self, batch: list[bytes]) -> list[bytes]:
        # return one result (bytes) per input; run the GPU forward pass on the whole batch
        return [b"REPLACE_ME" for _ in batch]
