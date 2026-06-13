from __future__ import annotations

import asyncio
from pathlib import Path

from ..errors import KeyNotFound, SigningUnsupported


class LocalObjectStore:
    """Filesystem-backed reference ObjectStore."""

    def __init__(self, *, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        def _write() -> None:
            p = self._path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        def _read() -> bytes:
            p = self._path(key)
            if not p.is_file():
                raise KeyNotFound(key)
            return p.read_bytes()

        return await asyncio.to_thread(_read)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, True)  # missing_ok=True

    async def signed_url(self, key: str, *, method: str = "GET", expires_s: int) -> str:
        raise SigningUnsupported(f"local store cannot sign URLs (key={key})")

    async def list_keys(self, prefix: str) -> list[str]:
        def _walk() -> list[str]:
            out = []
            for p in self._root.rglob("*"):
                if p.is_file():
                    key = p.relative_to(self._root).as_posix()
                    if key.startswith(prefix):
                        out.append(key)
            return sorted(out)

        return await asyncio.to_thread(_walk)
