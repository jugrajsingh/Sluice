from __future__ import annotations

import json
from collections.abc import Iterator

import httpx


class CliError(Exception):
    """A user-facing error: printed as a one-line message, no traceback."""


class AdminClient:
    def __init__(self, api: str, api_key: str | None, *, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._c = httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=30, transport=transport)

    @staticmethod
    def _raise_for_status(r: httpx.Response, path: str) -> None:
        if r.status_code == 401:
            raise CliError("unauthorized — set SLUICE_API_KEY / --api-key or `sluice config set-context`")
        if r.status_code == 404:
            raise CliError(f"not found: {path} — run 'sluice get' to list apps")
        if not r.is_success:
            raise CliError(f"{r.status_code}: {r.text.strip()[:300]}")

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        """Send a request, translating transport errors to CliError (no status checks)."""
        try:
            return self._c.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise CliError(
                f"cannot reach {self._c.base_url} — is --api the console/admin URL and is it reachable? ({e})"
            ) from e
        except httpx.HTTPError as e:
            raise CliError(f"request failed: {e}") from e

    def _do(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._request(method, path, **kw)
        self._raise_for_status(r, path)
        return r

    def list_apps(self) -> list[dict]:
        return self._do("GET", "/v1/apps").json()

    def get_app(self, name: str) -> dict:
        return self._do("GET", f"/v1/apps/{name}").json()

    def get_spec(self, name: str) -> str | None:
        """Return the stored spec YAML, or None when the app does not exist (404)."""
        path = f"/v1/apps/{name}/spec"
        r = self._request("GET", path)
        if r.status_code == 404:
            return None
        self._raise_for_status(r, path)
        return r.text

    def apply(self, name: str, yaml_text: str) -> dict:
        return self._do(
            "PUT", f"/v1/apps/{name}", content=yaml_text, headers={"content-type": "application/yaml"}
        ).json()

    def delete(self, name: str) -> None:
        self._do("DELETE", f"/v1/apps/{name}")

    def lifecycle(self, name: str, verb: str) -> None:
        self._do("POST", f"/v1/apps/{name}/{verb}")

    def server_version(self) -> str:
        """Best-effort server version for `sluice version`; never raises."""
        try:
            r = self._request("GET", "/healthz/version")
        except CliError:
            return "unreachable"
        if not r.is_success:
            return "unknown"
        try:
            data = r.json()
        except json.JSONDecodeError:
            return "unknown"
        return data.get("version", "unknown")

    def stream_logs(self, name: str, **q) -> Iterator[bytes]:
        """Stream worker log bytes; supports follow. Errors are translated to CliError."""
        path = f"/v1/apps/{name}/logs"
        params = {k: v for k, v in q.items() if v is not None}
        try:
            with self._c.stream("GET", path, params=params) as r:
                if not r.is_success:
                    r.read()
                    self._raise_for_status(r, path)
                yield from r.iter_bytes()
        except httpx.ConnectError as e:
            raise CliError(
                f"cannot reach {self._c.base_url} — is --api the console/admin URL and is it reachable? ({e})"
            ) from e
        except httpx.HTTPError as e:
            raise CliError(f"request failed: {e}") from e
