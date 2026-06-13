from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sluice_core.auth import TokenError, verify_worker_token
from sluice_core.models import Message

_bearer = HTTPBearer(auto_error=False)


class LeaseIn(BaseModel):
    max: int = 4


class ExtendIn(BaseModel):
    lease_ids: list[str]


class IdIn(BaseModel):
    lease_id: str


def build_broker_router(*, queue, objects, signing_key: str, lease_visibility_s: int = 120, url_ttl_s: int = 900):
    """Worker-facing broker. The app/source and worker identity come from the JWT, never the body.

    Stateless across gateway replicas: a lease_id is the queue ack_token, so any replica can
    ack/extend it.
    """
    r = APIRouter(prefix="/internal/v1")

    def claims(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
        if creds is None:
            raise HTTPException(status_code=401, detail="missing token")
        try:
            return verify_worker_token(creds.credentials, key=signing_key)
        except TokenError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    @r.post("/lease")
    async def lease(payload: LeaseIn, c: dict = Depends(claims)):
        app = c["app"]
        msgs = await queue.receive(app, max_messages=payload.max, wait_seconds=0)
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
            await queue.extend_lease(c["app"], Message(id=lid, body=b"", ack_token=lid), lease_visibility_s)
        return {"ok": True}

    @r.post("/ack")
    async def ack(payload: IdIn, c: dict = Depends(claims)):
        await queue.ack(c["app"], Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id))
        return {"ok": True}

    @r.post("/nack")
    async def nack(payload: IdIn, c: dict = Depends(claims)):
        await queue.nack(c["app"], Message(id=payload.lease_id, body=b"", ack_token=payload.lease_id))
        return {"ok": True}

    return r
