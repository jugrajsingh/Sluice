from __future__ import annotations

import logging

from aiohttp import web

from .metrics import render

# Probe endpoints that fire every few seconds (k8s liveness/readiness + Prometheus scrapes). Their
# access-log lines are pure noise and bury the real reconcile signal, so we drop them.
_QUIET_PATHS = ("/healthz", "/metrics")


class _QuietProbeFilter(logging.Filter):
    """Drop aiohttp access-log records for the probe/scrape endpoints (still log everything else)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in _QUIET_PATHS)


def build_health_app() -> web.Application:
    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def metrics(_request: web.Request) -> web.Response:
        return web.Response(body=render(), content_type="text/plain")

    app = web.Application()
    app.add_routes([web.get("/healthz", healthz), web.get("/metrics", metrics)])
    return app


async def start_health_server(port: int) -> web.AppRunner:
    # Suppress the per-hit access log for /healthz and /metrics — they are polled continuously and
    # otherwise drown the autoscaler's reconcile/VM-lifecycle events in 200-line spam.
    logging.getLogger("aiohttp.access").addFilter(_QuietProbeFilter())
    runner = web.AppRunner(build_health_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
