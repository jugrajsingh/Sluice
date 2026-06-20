"""Batch job API router — create/upload-url/submit/status.

Routes:
    POST /v1/{app}/batch                  → create job, return job_id
    POST /v1/{app}/batch/{job_id}/upload-url  → presigned PUT URL for one file
    POST /v1/{app}/batch/{job_id}/submit  → verify uploads, enqueue, transition state
    GET  /v1/{app}/batch/{job_id}         → aggregate status
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sluice_core.batch_models import BatchManifest
from sluice_core.batch_objects import BatchObjects
from sluice_core.batch_paths import validate_filename
from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import Queue

# Default SLA for batch jobs. Per-app override via registry is future work.
_DEFAULT_BATCH_SLA_HOURS = 24


class _UploadUrlIn(BaseModel):
    filename: str


class _SubmitIn(BaseModel):
    files: list[str]


def build_batch_router(
    *,
    queue: Queue,
    batch_objects: BatchObjects,
    registry: object | None,  # per-app batch config source; None ⇒ fall back to url_ttl_s
    url_ttl_s: int,
    now: Callable[[], float] = time.time,
) -> APIRouter:
    """Return a FastAPI APIRouter with the four batch-job routes."""

    router = APIRouter()

    async def _upload_ttl_s(app: str) -> int:
        # Per-app uploadTtlHours overrides the global presign TTL when the app declares a batch
        # block; otherwise (no registry, unknown app, or no batch block) fall back to url_ttl_s.
        if registry is None:
            return url_ttl_s
        spec = await registry.get_app(app)
        if spec is None or spec.batch is None:
            return url_ttl_s
        return spec.batch.upload_ttl_hours * 3600

    @router.post("/v1/{app}/batch")
    async def create_batch_job(app: str) -> dict[str, str]:
        job_id = uuid4().hex
        manifest = BatchManifest(
            job_id=job_id,
            app=app,
            state="pending_upload",
            files=[],
            created_at=now(),
            sla_hours=_DEFAULT_BATCH_SLA_HOURS,
        )
        await batch_objects.put_manifest(manifest)
        return {"job_id": job_id}

    @router.post("/v1/{app}/batch/{job_id}/upload-url")
    async def get_upload_url(app: str, job_id: str, body: _UploadUrlIn) -> dict[str, str]:
        # Reject an unsafe filename at the request boundary BEFORE minting a presigned
        # PUT — a traversal/separator name must never reach the object store (C3).
        try:
            validate_filename(body.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        url = await batch_objects.presign_input_put(app, job_id, body.filename, expires_s=await _upload_ttl_s(app))
        return {"url": url, "filename": body.filename}

    @router.post("/v1/{app}/batch/{job_id}/submit")
    async def submit_batch_job(app: str, job_id: str, body: _SubmitIn) -> dict[str, int]:
        # Validate everything BEFORE enqueuing anything (M2) so a bad submit never
        # leaves orphan messages on the batch queue:
        #   1. each filename is a safe single segment (C3),
        #   2. the job manifest exists (404 otherwise),
        #   3. every declared file is present in the store.
        # Only once all checks pass do we enqueue and transition the manifest.
        for filename in body.files:
            try:
                validate_filename(filename)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            manifest = await batch_objects.get_manifest(app, job_id)
        except KeyNotFound:
            raise HTTPException(status_code=404, detail="unknown batch job") from None

        for filename in body.files:
            if not await batch_objects.input_exists(app, job_id, filename):
                raise HTTPException(status_code=400, detail=f"File not uploaded: {filename}")

        dest = f"{app}-batch"
        for filename in body.files:
            await queue.enqueue(dest, b"", attributes={"job_id": job_id, "file": filename})

        updated = manifest.model_copy(update={"state": "running", "files": list(body.files)})
        await batch_objects.put_manifest(updated)

        return {"submitted": len(body.files)}

    @router.get("/v1/{app}/batch/{job_id}")
    async def batch_job_status(app: str, job_id: str) -> dict[str, object]:
        agg = await batch_objects.aggregate_status(app, job_id)
        return agg.model_dump()

    @router.get("/v1/{app}/batch/{job_id}/output")
    async def batch_output_urls(app: str, job_id: str) -> dict[str, object]:
        # Presigned GET per gzipped output part — the client downloads the .gz directly from the
        # store and inflates locally (no store creds, no proxying MBs through the gateway).
        keys = await batch_objects.list_output_keys(app, job_id)
        parts = [{"key": k, "url": await batch_objects.signed_get_output(k, expires_s=url_ttl_s)} for k in keys]
        return {"parts": parts}

    return router
