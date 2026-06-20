from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sluice_core.api_auth import add_api_key_auth
from sluice_core.app_yaml import parse_app_yaml, serialize_app_yaml
from sluice_core.interfaces import AppRegistry, ClusterInspector, NoWorkerPods, Queue

from .view import AppDetail, AppView, scale_status

_audit = logging.getLogger("sluice.audit")


def _console_version() -> str:
    try:
        return _pkg_version("sluice-console")
    except PackageNotFoundError:
        return "unknown"


def _counts(workers) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in workers:
        out[w.state.value] = out.get(w.state.value, 0) + 1
    return out


def build_console_app(
    *, registry: AppRegistry, queue: Queue, inspector: ClusterInspector, api_key: str | None = None
) -> FastAPI:
    app = FastAPI(title="sluice-console")
    # X-API-Key on the app-management API (apply/delete/pause/resume/drain + reads). /healthz open.
    add_api_key_auth(app, api_key, exempt_prefixes=("/healthz",))

    async def _view(a) -> AppView:
        # Live, fresh signal: worker counts + queue depth from the cluster/queue right now.
        workers = await inspector.workers(a)
        depth = await queue.depth(a.queue_ref)
        # Authoritative verdict: the controller's persisted AppStatus (phase/reason/candidate). It is the
        # source of truth for *why* an app is (not) scaling; counts above stay live. None when never written.
        status = await registry.get_status(a.name)
        return AppView(
            name=a.name,
            desired_state=a.desired_state,
            phase=status.phase if status else None,
            reason=status.reason if status else None,
            candidate=status.candidate if status else None,
            updated_at=status.updated_at if status else 0.0,
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

    @app.get("/v1/apps/{name}/spec")
    async def get_spec(name: str) -> Response:
        """Return the stored App spec as re-appliable YAML (used by `apply --dry-run` to diff)."""
        a = await registry.get_app(name)
        if a is None:
            raise HTTPException(404, "not found")
        return Response(serialize_app_yaml(a), media_type="application/yaml")

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

    @app.get("/v1/apps/{name}/logs")
    async def logs(
        name: str,
        worker: str | None = None,
        since: int | None = None,
        tail: int = 200,
        follow: bool = False,
    ) -> StreamingResponse:
        a = await registry.get_app(name)
        if a is None:
            raise HTTPException(404, "not found")
        gen = inspector.pod_logs(a, pod=worker, since_seconds=since, tail=tail, follow=follow)
        # Peek the first chunk so a NoWorkerPods (e.g. VM-backed or zero-scale app) becomes a clean
        # 400 *before* the streaming response's 200 headers are committed.
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            first = None  # no log output
        except NoWorkerPods as e:
            raise HTTPException(400, str(e)) from e

        async def _body():
            if first is not None:
                yield first
            async for chunk in gen:
                yield chunk

        return StreamingResponse(_body(), media_type="text/plain")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/healthz/version")
    async def version() -> dict[str, str]:
        return {"version": _console_version()}

    return app
