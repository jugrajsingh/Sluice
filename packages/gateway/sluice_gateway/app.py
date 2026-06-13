from __future__ import annotations

import asyncio
import base64
import uuid

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import InferenceObjects, Queue

from .metrics import CACHE_HITS, ENQUEUES, SYNC_HITS, render
from .util import content_hash, eta_seconds


class BatchIn(BaseModel):
    inputs: list[str]  # base64-encoded inputs


def build_app(
    *,
    queue: Queue,
    objects: InferenceObjects,
    t_sync_s: int = 3,
    throughput_per_s: float = 2.0,
    min_retry_s: int = 5,
    signing_key: str | None = None,
    lease_visibility_s: int = 120,
    url_ttl_s: int = 900,
) -> FastAPI:
    app = FastAPI(title="sluice-gateway")

    async def _try_result(model: str, rid: str) -> bytes | None:
        try:
            return await objects.get_result(model, rid)
        except KeyNotFound:
            return None

    @app.post("/v1/{model}/infer")
    async def infer(model: str, request: Request) -> Response:
        body = await request.body()
        rid = content_hash(body)

        cached = await _try_result(model, rid)
        if cached is not None:
            CACHE_HITS.labels(app=model).inc()
            return Response(content=cached, status_code=200, media_type="application/octet-stream")

        await objects.put_request(model, rid, body)
        await queue.enqueue(model, rid.encode())
        ENQUEUES.labels(app=model).inc()

        deadline = asyncio.get_event_loop().time() + t_sync_s
        while asyncio.get_event_loop().time() < deadline:
            res = await _try_result(model, rid)
            if res is not None:
                SYNC_HITS.labels(app=model).inc()
                return Response(content=res, status_code=200, media_type="application/octet-stream")
            await asyncio.sleep(0.05)

        depth = await queue.depth(model)
        eta = eta_seconds(visible=depth.visible, throughput_per_s=throughput_per_s, min_s=min_retry_s)
        return Response(
            status_code=202,
            headers={"Retry-After": str(eta)},
            media_type="application/json",
            content=f'{{"ticket":"{rid}","retry_after":{eta}}}',
        )

    @app.get("/v1/{model}/status/{ticket}")
    async def status(model: str, ticket: str) -> Response:
        res = await _try_result(model, ticket)
        if res is not None:
            return Response(content=res, status_code=200, media_type="application/octet-stream")
        depth = await queue.depth(model)
        eta = eta_seconds(visible=depth.visible, throughput_per_s=throughput_per_s, min_s=min_retry_s)
        return Response(status_code=202, headers={"Retry-After": str(eta)})

    @app.post("/v1/{model}/batch")
    async def batch(model: str, payload: BatchIn) -> Response:
        bid = uuid.uuid4().hex
        for b64 in payload.inputs:
            data = base64.b64decode(b64)
            rid = content_hash(data)
            await objects.put_request(model, rid, data)
            await queue.enqueue(model, rid.encode(), attributes={"batch_id": bid})
        return Response(
            status_code=202,
            media_type="application/json",
            content=f'{{"batch_id":"{bid}","submitted":{len(payload.inputs)}}}',
        )

    @app.get("/v1/{model}/batch/{bid}")
    async def batch_status(model: str, bid: str) -> Response:
        depth = await queue.depth(model)
        completed = depth.visible == 0 and depth.in_flight == 0
        return Response(
            status_code=200,
            media_type="application/json",
            content=f'{{"completed":{str(completed).lower()},"output_prefix":"apps/{model}/results"}}',
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=render(), media_type="text/plain")

    if signing_key:
        from .broker import build_broker_router

        app.include_router(
            build_broker_router(
                queue=queue,
                objects=objects,
                signing_key=signing_key,
                lease_visibility_s=lease_visibility_s,
                url_ttl_s=url_ttl_s,
            )
        )

    return app
