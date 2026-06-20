from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request, Response
from sluice_core.api_auth import add_api_key_auth
from sluice_core.batch_objects import BatchObjects
from sluice_core.compression import gunzip, is_gzip
from sluice_core.errors import KeyNotFound
from sluice_core.fingerprint import request_fingerprint
from sluice_core.interfaces import InferenceObjects, Queue
from sluice_core.vm_objects import VmObjects

from .metrics import CACHE_HITS, ENQUEUES, SYNC_HITS, render
from .util import eta_seconds


def _result_response(res: bytes, accept_encoding: str) -> Response:
    """Serve a stored result body, honoring its gzip-if-smaller encoding.

    Results are stored compressed-when-smaller (sluice_core.compression); the bytes self-identify via
    the gzip magic header. A client that accepts gzip gets the compressed bytes with
    `Content-Encoding: gzip` (small wire, transparent decompression by the HTTP layer); a client that
    does not gets it gunzipped. The encoding is set on the gateway's OWN response, never on the bucket
    object — so behavior is identical across object stores (no per-store transcoding).
    """
    if is_gzip(res):
        if "gzip" in accept_encoding.lower():
            return Response(
                content=res,
                status_code=200,
                media_type="application/octet-stream",
                headers={"Content-Encoding": "gzip"},
            )
        res = gunzip(res)
    return Response(content=res, status_code=200, media_type="application/octet-stream")


def build_app(
    *,
    queue: Queue,
    objects: InferenceObjects,
    t_sync_s: int = 3,
    throughput_per_s: float = 2.0,
    min_retry_s: int = 5,
    signing_key: str | None = None,
    api_key: str | None = None,
    lease_visibility_s: int = 120,
    url_ttl_s: int = 900,
    batch_objects: BatchObjects | None = None,
    batch_queue: Queue | None = None,
    batch_lease_visibility_s: int = 900,
    vm_objects: VmObjects | None = None,
    registry: object | None = None,
) -> FastAPI:
    app = FastAPI(title="sluice-gateway")
    # X-API-Key on the public admission API. /healthz + /metrics (in-cluster scrape) stay open, and
    # /internal is the JWT broker (workers authenticate with their minted token, not this key).
    add_api_key_auth(app, api_key, exempt_prefixes=("/healthz", "/metrics", "/internal"))

    async def _try_result(model: str, rid: str) -> bytes | None:
        try:
            return await objects.get_result(model, rid)
        except KeyNotFound:
            return None

    @app.post("/v1/{model}/infer")
    async def infer(model: str, request: Request) -> Response:
        body = await request.body()
        rid = request_fingerprint(body)

        cached = await _try_result(model, rid)
        if cached is not None:
            CACHE_HITS.labels(app=model).inc()
            return _result_response(cached, request.headers.get("accept-encoding", ""))

        await objects.put_request(model, rid, body)
        await queue.enqueue(f"{model}-infer", rid.encode())
        ENQUEUES.labels(app=model).inc()

        deadline = asyncio.get_event_loop().time() + t_sync_s
        while asyncio.get_event_loop().time() < deadline:
            res = await _try_result(model, rid)
            if res is not None:
                SYNC_HITS.labels(app=model).inc()
                return _result_response(res, request.headers.get("accept-encoding", ""))
            await asyncio.sleep(0.05)

        depth = await queue.depth(f"{model}-infer")
        eta = eta_seconds(visible=depth.visible, throughput_per_s=throughput_per_s, min_s=min_retry_s)
        return Response(
            status_code=202,
            headers={"Retry-After": str(eta)},
            media_type="application/json",
            content=f'{{"ticket":"{rid}","retry_after":{eta}}}',
        )

    @app.get("/v1/{model}/status/{ticket}")
    async def status(model: str, ticket: str, request: Request) -> Response:
        res = await _try_result(model, ticket)
        if res is not None:
            return _result_response(res, request.headers.get("accept-encoding", ""))
        depth = await queue.depth(f"{model}-infer")
        eta = eta_seconds(visible=depth.visible, throughput_per_s=throughput_per_s, min_s=min_retry_s)
        return Response(status_code=202, headers={"Retry-After": str(eta)})

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=render(), media_type="text/plain")

    if signing_key:
        from .broker import build_broker_router

        # When the batch lane is configured (batch_objects + a long-window batch_queue),
        # the broker exposes /internal/v1/batch/* so VM workers can lease batch files and
        # fetch input via a presigned GET (C1) — Redis is never reachable from a burst VM.
        app.include_router(
            build_broker_router(
                queue=queue,
                objects=objects,
                signing_key=signing_key,
                lease_visibility_s=lease_visibility_s,
                url_ttl_s=url_ttl_s,
                batch_queue=batch_queue,
                batch_objects=batch_objects,
                batch_lease_visibility_s=batch_lease_visibility_s,
                vm_objects=vm_objects,
            )
        )

    if batch_objects is not None:
        from .batch import build_batch_router

        app.include_router(
            build_batch_router(
                queue=queue,
                batch_objects=batch_objects,
                registry=registry,
                url_ttl_s=url_ttl_s,
                now=time.time,
            )
        )

    return app
