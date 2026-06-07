from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, Response
from sluice_core.app_yaml import parse_app_yaml
from sluice_core.interfaces import AppRegistry, ClusterInspector, Queue

from .view import AppDetail, AppView, scale_status

_audit = logging.getLogger("sluice.audit")


def _counts(workers) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in workers:
        out[w.state.value] = out.get(w.state.value, 0) + 1
    return out


def build_console_app(*, registry: AppRegistry, queue: Queue, inspector: ClusterInspector) -> FastAPI:
    app = FastAPI(title="sluice-console")

    async def _view(a) -> AppView:
        workers = await inspector.workers(a)
        depth = await queue.depth(a.queue_ref)
        return AppView(
            name=a.name,
            desired_state=a.desired_state,
            scale_status=scale_status(a, workers, visible=depth.visible),
            queue=depth,
            workers=_counts(workers),
        )

    @app.get("/v1/apps")
    async def list_apps() -> list[AppView]:
        return [await _view(a) for a in await registry.list_apps()]

    @app.get("/v1/apps/{name}")
    async def get_app(name: str) -> AppDetail:
        a = await registry.get_app(name)
        if a is None:
            raise HTTPException(404, "not found")
        base = await _view(a)
        workers = await inspector.workers(a)
        return AppDetail(**base.model_dump(), worker_list=workers)

    @app.put("/v1/apps/{name}")
    async def apply_app(name: str, request: Request) -> dict[str, str]:
        try:
            spec = parse_app_yaml((await request.body()).decode())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if spec.name != name:
            raise HTTPException(422, f"metadata.name {spec.name!r} != path name {name!r}")
        _audit.info("action=apply app=%s", name)
        await registry.put_app(spec)
        return {"applied": name}

    @app.delete("/v1/apps/{name}")
    async def remove_app(name: str) -> dict[str, str]:
        _audit.info("action=delete app=%s", name)
        await registry.delete_app(name)
        return {"deleted": name}

    @app.post("/v1/apps/{name}/pause")
    async def pause(name: str) -> Response:
        _audit.info("action=pause app=%s", name)
        await registry.set_desired_state(name, "Paused")
        return Response(status_code=200, content='{"desiredState":"Paused"}', media_type="application/json")

    @app.post("/v1/apps/{name}/resume")
    async def resume(name: str) -> Response:
        _audit.info("action=resume app=%s", name)
        await registry.set_desired_state(name, "Ready")
        return Response(status_code=200, content='{"desiredState":"Ready"}', media_type="application/json")

    @app.post("/v1/apps/{name}/drain")
    async def drain(name: str) -> Response:
        # MVP: drain == pause (stop new work, let in-flight finish). Dedicated drain flag is fast-follow.
        _audit.info("action=drain app=%s", name)
        await registry.set_desired_state(name, "Paused")
        return Response(status_code=202, content='{"draining":true}', media_type="application/json")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
