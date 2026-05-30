from __future__ import annotations

from ..app_yaml import parse_app_yaml, serialize_app_yaml
from ..errors import KeyNotFound
from ..interfaces import ObjectStore
from ..models import AppSpec, AppStatus


class ObjectStoreAppRegistry:
    """AppRegistry over any ObjectStore (kops-style state store)."""

    def __init__(self, *, store: ObjectStore, root: str = "sluice") -> None:
        self._store = store
        self._root = root

    def _spec_key(self, name: str) -> str:
        return f"{self._root}/apps/{name}/spec.yaml"

    def _status_key(self, name: str) -> str:
        return f"{self._root}/apps/{name}/status.json"

    async def list_apps(self) -> list[AppSpec]:
        keys = await self._store.list_keys(f"{self._root}/apps/")
        out = []
        for k in keys:
            if k.endswith("/spec.yaml"):
                out.append(parse_app_yaml((await self._store.get(k)).decode()))
        return sorted(out, key=lambda a: a.name)

    async def get_app(self, name: str) -> AppSpec | None:
        try:
            return parse_app_yaml((await self._store.get(self._spec_key(name))).decode())
        except KeyNotFound:
            return None

    async def put_app(self, spec: AppSpec) -> None:
        await self._store.put(
            self._spec_key(spec.name), serialize_app_yaml(spec).encode(), content_type="application/yaml"
        )

    async def delete_app(self, name: str) -> None:
        await self._store.delete(self._spec_key(name))
        await self._store.delete(self._status_key(name))

    async def set_desired_state(self, name: str, state: str) -> None:
        app = await self.get_app(name)
        if app is None:
            raise KeyNotFound(name)
        await self.put_app(app.model_copy(update={"desired_state": state}))

    async def write_status(self, name: str, status: AppStatus) -> None:
        await self._store.put(
            self._status_key(name), status.model_dump_json().encode(), content_type="application/json"
        )

    async def get_status(self, name: str) -> AppStatus | None:
        try:
            return AppStatus.model_validate_json(await self._store.get(self._status_key(name)))
        except KeyNotFound:
            return None
