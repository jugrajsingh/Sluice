"""Reusable AppRegistry conformance tests. Subclass and provide a `registry` fixture."""

from __future__ import annotations

import pytest

from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import AppRegistry
from sluice_core.models import AppSpec, AppStatus


def _spec(name: str = "m") -> AppSpec:
    return AppSpec(name=name, image="repo/x:1", handler="h:H")


class RegistryConformance:
    @pytest.fixture
    def registry(self) -> AppRegistry:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_satisfies_protocol(self, registry: AppRegistry) -> None:
        assert isinstance(registry, AppRegistry)

    async def test_put_get_roundtrip(self, registry: AppRegistry) -> None:
        await registry.put_app(_spec("a"))
        got = await registry.get_app("a")
        assert got == _spec("a")

    async def test_get_missing_returns_none(self, registry: AppRegistry) -> None:
        assert await registry.get_app("ghost") is None

    async def test_list_sorted(self, registry: AppRegistry) -> None:
        await registry.put_app(_spec("b"))
        await registry.put_app(_spec("a"))
        assert [a.name for a in await registry.list_apps()] == ["a", "b"]

    async def test_set_desired_state(self, registry: AppRegistry) -> None:
        await registry.put_app(_spec("s"))
        await registry.set_desired_state("s", "Paused")
        assert (await registry.get_app("s")).desired_state == "Paused"

    async def test_set_desired_state_missing_raises(self, registry: AppRegistry) -> None:
        with pytest.raises(KeyNotFound):
            await registry.set_desired_state("ghost", "Paused")

    async def test_status_roundtrip(self, registry: AppRegistry) -> None:
        await registry.put_app(_spec("st"))
        await registry.write_status("st", AppStatus(phase="Held", reason="stockout"))
        got = await registry.get_status("st")
        assert got.phase == "Held" and got.reason == "stockout"
        assert await registry.get_status("ghost") is None

    async def test_delete_removes_and_is_idempotent(self, registry: AppRegistry) -> None:
        await registry.put_app(_spec("d"))
        await registry.delete_app("d")
        await registry.delete_app("d")
        assert await registry.get_app("d") is None
