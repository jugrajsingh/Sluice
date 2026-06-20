from __future__ import annotations

import asyncio
import contextlib
import gzip
import os
import shutil
import tempfile
from typing import Any

from sluice_core.batch_models import BatchFileStatus


def _gzip_to_temp(src: str) -> str:
    """Stream-gzip ``src`` to a fresh temp file (1 MiB chunks → bounded RAM). Returns the .gz path."""
    fd, dst = tempfile.mkstemp(prefix="sluice-batch-", suffix=".jsonl.gz")
    os.close(fd)
    with open(src, "rb") as fin, gzip.open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    return dst


def _unlink_quiet(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


class BrokerBatchWriter:
    """Implements the BatchFileProcessor ``_Writer`` protocol entirely through the gateway broker.

    The worker holds no object-store credentials (ADR-002): output parts go to a broker-minted
    presigned PUT (large, streamed from a spilled temp file, gzip-compressed for storage); per-file
    status is proxied through the broker (small JSON). ``app`` is informational — the broker re-derives
    every key from the JWT app claim, so it is authoritative.
    """

    def __init__(self, broker: Any, *, app: str) -> None:
        self._broker = broker
        self._app = app

    async def put_output_part(
        self, app: str, job_id: str, filename: str, start_offset: int, body: bytes | os.PathLike[str] | str
    ) -> None:
        # body is the spilled temp-file path written by BatchFileProcessor. Gzip it (storage
        # conservation — the client gunzips after download; the gzip magic header self-identifies it)
        # then stream the compressed file to a fresh presigned PUT minted right now (URLs expire, so we
        # never mint ahead of the flush). All file I/O is off-thread to never block the event loop.
        url = await self._broker.batch_output_url(job_id, filename, start_offset)
        gz_path = await asyncio.to_thread(_gzip_to_temp, os.fspath(body))
        try:
            await self._broker.put_file(url, gz_path)
        finally:
            await asyncio.to_thread(_unlink_quiet, gz_path)

    async def put_file_status(self, app: str, job_id: str, st: BatchFileStatus) -> None:
        await self._broker.batch_status_put(job_id, st.file, st.model_dump())

    async def get_file_status(self, app: str, job_id: str, filename: str) -> BatchFileStatus | None:
        raw = await self._broker.batch_status_get(job_id, filename)
        return BatchFileStatus.model_validate(raw) if raw is not None else None
