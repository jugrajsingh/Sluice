from __future__ import annotations

import json

from sluice_core.interfaces import ObjectStore
from sluice_core.vm_paths import desired_key


class VmCommander:
    """Writes controller->agent commands into the bucket channel."""

    def __init__(self, *, store: ObjectStore, root: str = "sluice") -> None:
        self._store = store
        self._root = root

    async def command(self, app: str, vm_id: str, action: str) -> None:
        await self._store.put(
            desired_key(app, vm_id, root=self._root),
            json.dumps({"action": action}).encode(),
            content_type="application/json",
        )
