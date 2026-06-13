"""Example BYO-model handler. CPU-friendly + deterministic so the demo runs anywhere.
Swap load()/predict() for your real Detectron2/SAM2/ONNX model on a GPU node."""

from __future__ import annotations

import hashlib
import json

from sluice_worker.handler import BaseHandler


class SegHandler(BaseHandler):
    async def load(self) -> None:
        await super().load()
        # self.model = load_model("/mnt/models/...").to("cuda")   # real version

    async def predict(self, batch: list[bytes]) -> list[bytes]:
        results = []
        for data in batch:
            h = hashlib.sha256(data).hexdigest()
            # fake but deterministic "mask" payload; real version returns RLE/polygons
            results.append(json.dumps({"hash": h, "boxes": [[0, 0, 10, 10]], "score": 0.99}).encode())
        return results
