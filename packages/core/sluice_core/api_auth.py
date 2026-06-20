"""Shared X-API-Key gate for the control-plane FastAPI services.

Guards the public admission API (gateway) and the app-management API (console) with a static
`X-API-Key` header. No-op when no key is configured, so it is backward compatible. Health probes,
`/metrics` (scraped in-cluster by Prometheus over the pod IP), and the gateway's `/internal` JWT
broker are exempt. fastapi/starlette are imported lazily so this module never forces a web
dependency onto sluice_core's surface — only the callers (gateway/console) have them installed.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def add_api_key_auth(
    app: FastAPI,
    api_key: str | None,
    *,
    exempt_prefixes: tuple[str, ...] = ("/healthz", "/metrics"),
) -> None:
    """Add an X-API-Key middleware to `app`. If `api_key` is falsy, do nothing (open).

    A request is allowed when its path equals or is under an exempt prefix, or it presents the
    matching key (constant-time compared). Otherwise it gets 401.
    """
    if not api_key:
        return

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def _require_key(request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in exempt_prefixes):
            return await call_next(request)
        if not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key):
            return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
        return await call_next(request)
