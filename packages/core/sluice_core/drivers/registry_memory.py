from __future__ import annotations

from ..errors import KeyNotFound
from ..models import AppSpec, AppStatus


class MemoryAppRegistry:
    """In-process reference AppRegistry."""

    def __init__(self) -> None:
        self._specs: dict[str, AppSpec] = {}
        self._status: dict[str, AppStatus] = {}

    async def list_apps(self) -> list[AppSpec]:
        return sorted(self._specs.values(), key=lambda a: a.name)

    async def get_app(self, name: str) -> AppSpec | None:
        return self._specs.get(name)

    async def put_app(self, spec: AppSpec) -> None:
        self._specs[spec.name] = spec

    async def delete_app(self, name: str) -> None:
        self._specs.pop(name, None)
        self._status.pop(name, None)

    async def set_desired_state(self, name: str, state: str) -> None:
        app = self._specs.get(name)
        if app is None:
            raise KeyNotFound(name)
        self._specs[name] = app.model_copy(update={"desired_state": state})

    async def write_status(self, name: str, status: AppStatus) -> None:
        self._status[name] = status

    async def get_status(self, name: str) -> AppStatus | None:
        return self._status.get(name)
