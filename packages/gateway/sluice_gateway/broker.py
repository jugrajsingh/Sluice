from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sluice_core.auth import TokenError, verify_worker_token
from sluice_core.batch_models import BatchFileStatus
from sluice_core.batch_paths import validate_filename
from sluice_core.models import Message

_bearer = HTTPBearer(auto_error=False)


class LeaseIn(BaseModel):
    max: int = 4


class ExtendIn(BaseModel):
    lease_ids: list[str]


class IdIn(BaseModel):
    lease_id: str


class OutputUrlIn(BaseModel):
    job_id: str
    file: str
    start_offset: int


class StatusPutIn(BaseModel):
    job_id: str
    file: str
    status: dict


class HeartbeatIn(BaseModel):
    vm_id: str
    phase: str
    workers: int


def build_broker_router(
    *,
    queue,
    objects,
    signing_key: str,
    lease_visibility_s: int = 120,
    url_ttl_s: int = 900,
    batch_queue=None,
    batch_objects=None,
    batch_lease_visibility_s: int = 900,
    vm_objects=None,
):
    """Worker-facing broker. The app/source and worker identity come from the JWT, never the body.

    Stateless across gateway replicas: a lease_id is the queue ack_token, so any replica can
    ack/extend it.

    When ``batch_queue`` and ``batch_objects`` are supplied, the batch lane (spec §5 / C1) is
    mounted at ``/internal/v1/batch/*``. A batch lease pulls one ``{app}-batch`` message per item
    and returns a presigned GET (``body_url``) over the file's ``input_key`` so a VM worker — which
    cannot reach in-cluster Redis or hold store credentials — can fetch the JSONL body directly.
    The ``{app}-batch`` queue is constructed with a LONG idle-reclaim window (``batch_lease_visibility_s``,
    M3) and ``/batch/extend`` resets that window from the worker's heartbeat so a multi-minute file is
    not reclaimed mid-process.
    """
    r = APIRouter(prefix="/internal/v1")

    def claims(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
        if creds is None:
            raise HTTPException(status_code=401, detail="missing token")
        try:
            return verify_worker_token(creds.credentials, key=signing_key)
        except TokenError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    def _infer_source(app: str) -> str:
        # Online lane queue, symmetric with the batch lane's {app}-batch (see _mount_batch_routes).
        return f"{app}-infer"

    @r.post("/lease")
    async def lease(payload: LeaseIn, c: dict = Depends(claims)):
        app = c["app"]
        msgs = await queue.receive(_infer_source(app), max_messages=payload.max, wait_seconds=0)
        items = []
        for m in msgs:
            rid = m.body.decode()
            items.append(
                {
                    "request_id": rid,
                    "lease_id": m.ack_token,
                    "body_url": await objects.signed_get_request(app, rid, expires_s=url_ttl_s),
                    "result_url": await objects.signed_put_result(app, rid, expires_s=url_ttl_s),
                }
            )
        return {"items": items, "visibility_s": lease_visibility_s}

    @r.post("/extend")
    async def extend(payload: ExtendIn, c: dict = Depends(claims)):
        for lid in payload.lease_ids:
            await queue.extend_lease(
                _infer_source(c["app"]), Message(id=lid, body=b"", ack_token=lid), lease_visibility_s
            )
        return {"ok": True}

    @r.post("/ack")
    async def ack(payload: IdIn, c: dict = Depends(claims)):
        await queue.ack(_infer_source(c["app"]), Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id))
        return {"ok": True}

    @r.post("/nack")
    async def nack(payload: IdIn, c: dict = Depends(claims)):
        await queue.nack(_infer_source(c["app"]), Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id))
        return {"ok": True}

    if batch_queue is not None and batch_objects is not None:
        _mount_batch_routes(
            r,
            claims=claims,
            batch_queue=batch_queue,
            batch_objects=batch_objects,
            batch_lease_visibility_s=batch_lease_visibility_s,
            url_ttl_s=url_ttl_s,
        )

    if vm_objects is not None:
        _mount_vm_routes(r, claims=claims, vm_objects=vm_objects)

    return r


def _mount_batch_routes(
    r: APIRouter,
    *,
    claims,
    batch_queue,
    batch_objects,
    batch_lease_visibility_s: int,
    url_ttl_s: int,
) -> None:
    """Mount the batch lane (lease/ack/extend/nack) on the broker router (spec §5 / C1)."""

    def _batch_source(app: str) -> str:
        return f"{app}-batch"

    @r.post("/batch/lease")
    async def batch_lease(payload: LeaseIn, c: dict = Depends(claims)):
        app = c["app"]
        source = _batch_source(app)
        msgs = await batch_queue.receive(source, max_messages=payload.max, wait_seconds=0)
        items = []
        for m in msgs:
            job_id = m.attributes.get("job_id", "")
            file = m.attributes.get("file", "")
            items.append(
                {
                    "lease_id": m.ack_token,
                    "job_id": job_id,
                    "file": file,
                    "body_url": await batch_objects.signed_get_input(app, job_id, file, expires_s=url_ttl_s),
                }
            )
        return {"items": items, "visibility_s": batch_lease_visibility_s}

    @r.post("/batch/extend")
    async def batch_extend(payload: ExtendIn, c: dict = Depends(claims)):
        source = _batch_source(c["app"])
        for lid in payload.lease_ids:
            await batch_queue.extend_lease(source, Message(id=lid, body=b"", ack_token=lid), batch_lease_visibility_s)
        return {"ok": True}

    @r.post("/batch/ack")
    async def batch_ack(payload: IdIn, c: dict = Depends(claims)):
        await batch_queue.ack(
            _batch_source(c["app"]), Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id)
        )
        return {"ok": True}

    @r.post("/batch/nack")
    async def batch_nack(payload: IdIn, c: dict = Depends(claims)):
        await batch_queue.nack(
            _batch_source(c["app"]), Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id)
        )
        return {"ok": True}

    @r.post("/batch/output-url")
    async def batch_output_url(payload: OutputUrlIn, c: dict = Depends(claims)):
        # Presigned PUT for ONE output part (large object → direct, never proxied). The key is derived
        # server-side from the JWT app claim + the validated file/offset, so a worker can never address
        # another app's data or an arbitrary key.
        try:
            validate_filename(payload.file)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        url = await batch_objects.signed_put_output_part(
            c["app"], payload.job_id, payload.file, payload.start_offset, expires_s=url_ttl_s
        )
        return {"url": url, "expires_s": url_ttl_s}

    @r.get("/batch/status")
    async def batch_status_get(job_id: str, file: str, c: dict = Depends(claims)):
        # Resume read (small JSON) — proxied through the gateway, which holds the store creds.
        try:
            validate_filename(file)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        st = await batch_objects.get_file_status(c["app"], job_id, file)
        return {"found": st is not None, "status": (st.model_dump() if st is not None else None)}

    @r.post("/batch/status")
    async def batch_status_put(payload: StatusPutIn, c: dict = Depends(claims)):
        # Checkpoint write (small JSON) — proxied; the broker re-derives the key from the app claim.
        try:
            validate_filename(payload.file)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await batch_objects.put_file_status(c["app"], payload.job_id, BatchFileStatus.model_validate(payload.status))
        return {"ok": True}


def _mount_vm_routes(r: APIRouter, *, claims, vm_objects) -> None:
    """Mount the VM control channel (spec §3): the VM agent holds no store creds, so it POSTs its
    heartbeat and GETs its command through the broker, which performs the tiny store writes."""

    @r.post("/vm/heartbeat")
    async def vm_heartbeat(payload: HeartbeatIn, c: dict = Depends(claims)):
        # Stamp the receive-time SERVER-SIDE (the VM has no trusted clock) — this is what the autoscaler
        # compares against `now` to detect a hung/unreachable VM (ADR-012). The worker payload itself
        # stays {phase, workers}; the gateway adds received_at.
        await vm_objects.put_heartbeat(
            c["app"],
            payload.vm_id,
            {"phase": payload.phase, "workers": payload.workers, "received_at": time.time()},
        )
        return {"ok": True}

    @r.get("/vm/command")
    async def vm_command(vm_id: str, c: dict = Depends(claims)):
        return {"command": await vm_objects.pop_command(c["app"], vm_id)}
