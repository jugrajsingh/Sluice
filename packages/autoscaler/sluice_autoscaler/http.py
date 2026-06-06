from __future__ import annotations

from aiohttp import web

from .metrics import render


def build_health_app() -> web.Application:
    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def metrics(_request: web.Request) -> web.Response:
        return web.Response(body=render(), content_type="text/plain")

    app = web.Application()
    app.add_routes([web.get("/healthz", healthz), web.get("/metrics", metrics)])
    return app


async def start_health_server(port: int) -> web.AppRunner:
    runner = web.AppRunner(build_health_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
