from __future__ import annotations

import abc


class BaseHandler(abc.ABC):
    """Implement load()/predict(); health() defaults to 'loaded'."""

    def __init__(self) -> None:
        self._loaded = False

    async def load(self) -> None:
        self._loaded = True

    @abc.abstractmethod
    async def predict(self, batch: list[bytes]) -> list[bytes]: ...

    async def health(self) -> bool:
        return self._loaded
