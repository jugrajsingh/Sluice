from __future__ import annotations

import json

from .errors import KeyNotFound
from .interfaces import ObjectStore
from .vm_paths import desired_key, heartbeat_key


class VmObjects:
    """Gateway-side VM control-channel access. The VM agent holds no store creds; it POSTs its
    heartbeat and GETs its command through the broker, which performs the tiny store writes here."""

    def __init__(self, store: ObjectStore, *, root: str = "sluice") -> None:
        self._store = store
        self._root = root

    async def put_heartbeat(self, app: str, vm_id: str, doc: dict[str, object]) -> None:
        await self._store.put(
            heartbeat_key(app, vm_id, root=self._root),
            json.dumps(doc).encode(),
            content_type="application/json",
        )

    async def pop_command(self, app: str, vm_id: str) -> str | None:
        key = desired_key(app, vm_id, root=self._root)
        try:
            raw = await self._store.get(key)
        except KeyNotFound:
            return None
        await self._store.delete(key)
        parsed = json.loads(raw)
        action = parsed.get("action") if isinstance(parsed, dict) else None
        return action if isinstance(action, str) else None
