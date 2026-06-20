"""Typed object-store access for the batch namespace.

Each VM worker writes only its own file's status object — no concurrent writers per key,
so no CAS is needed. The status endpoint calls aggregate_status() to read all per-file
status objects and produce a BatchJobAggregate.
"""

from __future__ import annotations

import json

from .batch_models import BatchFileStatus, BatchJobAggregate, BatchManifest
from .batch_paths import input_key, job_prefix, manifest_key, output_part_key, status_key
from .compression import gzip_if_smaller
from .errors import KeyNotFound
from .interfaces import ObjectStore


class BatchObjects:
    """Typed object-store façade for batch job artefacts."""

    def __init__(self, *, store: ObjectStore, prefix_template: str = "AppData/{app}") -> None:
        self._store = store
        self._prefix_template = prefix_template  # reserved for future multi-tenant prefixes

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    async def put_manifest(self, manifest: BatchManifest) -> None:
        key = manifest_key(manifest.app, manifest.job_id)
        await self._store.put(key, json.dumps(manifest.model_dump()).encode())

    async def get_manifest(self, app: str, job_id: str) -> BatchManifest:
        raw = await self._store.get(manifest_key(app, job_id))
        return BatchManifest.model_validate(json.loads(raw))

    # ------------------------------------------------------------------
    # Input presigning
    # ------------------------------------------------------------------

    async def presign_input_put(self, app: str, job_id: str, filename: str, *, expires_s: int) -> str:
        return await self._store.signed_url(input_key(app, job_id, filename), method="PUT", expires_s=expires_s)

    async def signed_get_input(self, app: str, job_id: str, filename: str, *, expires_s: int) -> str:
        """Presigned GET for a job's input file.

        The worker-facing broker hands this URL to a VM worker so it can fetch the
        JSONL body directly from the store without holding any store credentials —
        mirrors ``InferenceObjects.signed_get_request`` for the infer lane.
        ``input_key`` validates the filename, so a traversal name raises ``ValueError``.
        """
        return await self._store.signed_url(input_key(app, job_id, filename), method="GET", expires_s=expires_s)

    async def input_exists(self, app: str, job_id: str, filename: str) -> bool:
        return await self._store.exists(input_key(app, job_id, filename))

    # ------------------------------------------------------------------
    # Per-file status
    # ------------------------------------------------------------------

    async def put_file_status(self, app: str, job_id: str, status: BatchFileStatus) -> None:
        key = status_key(app, job_id, status.file)
        await self._store.put(key, json.dumps(status.model_dump()).encode())

    async def get_file_status(self, app: str, job_id: str, filename: str) -> BatchFileStatus | None:
        key = status_key(app, job_id, filename)
        if not await self._store.exists(key):
            return None
        raw = await self._store.get(key)
        return BatchFileStatus.model_validate(json.loads(raw))

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    async def aggregate_status(self, app: str, job_id: str) -> BatchJobAggregate:
        # The manifest is the source of truth for the full file set.
        try:
            manifest = await self.get_manifest(app, job_id)
            manifest_files: list[str] = manifest.files
        except KeyNotFound:
            # No manifest yet — fall back to the status listing (legacy / race path).
            manifest_files = []

        # Build a map of per-file status objects from the store.
        prefix = f"{job_prefix(app, job_id)}/status/"
        keys = await self._store.list_keys(prefix)
        status_map: dict[str, BatchFileStatus] = {}
        for key in keys:
            raw = await self._store.get(key)
            file_status = BatchFileStatus.model_validate(json.loads(raw))
            status_map[file_status.file] = file_status

        # Determine the authoritative file set: manifest when available, else status objects.
        file_set = manifest_files if manifest_files else list(status_map.keys())
        files_total = len(file_set)

        _TERMINAL = {"completed", "partial", "failed"}
        files_done = 0
        files_running = 0
        files_pending = 0
        for filename in file_set:
            entry: BatchFileStatus | None = status_map.get(filename)
            if entry is None or entry.state == "pending_upload":
                files_pending += 1
            elif entry.state == "running":
                files_running += 1
            elif entry.state in _TERMINAL:
                files_done += 1
            else:
                files_pending += 1

        statuses = list(status_map.values())
        records_done = sum(s.records_done for s in statuses)
        records_total = sum(s.records_total for s in statuses)
        records_failed = sum(s.records_failed for s in statuses)

        # Derive the job-level state.
        if files_done == files_total:
            agg_state = "partial" if records_failed > 0 else "completed"
        else:
            agg_state = "running"

        return BatchJobAggregate(
            state=agg_state,
            files_total=files_total,
            files_done=files_done,
            files_running=files_running,
            files_pending=files_pending,
            records_done=records_done,
            records_total=records_total,
            records_failed=records_failed,
            output_prefix=f"{job_prefix(app, job_id)}/output",
        )

    # ------------------------------------------------------------------
    # Output parts
    # ------------------------------------------------------------------

    async def signed_put_output_part(
        self, app: str, job_id: str, filename: str, start_offset: int, *, expires_s: int
    ) -> str:
        """Presigned PUT for one output part — the worker streams the part directly to the store
        (large object; never proxied through the gateway). Key is derived server-side."""
        key = output_part_key(app, job_id, filename, start_offset)
        return await self._store.signed_url(key, method="PUT", expires_s=expires_s)

    async def put_output_part(self, app: str, job_id: str, filename: str, start_offset: int, body: bytes) -> None:
        key = output_part_key(app, job_id, filename, start_offset)
        await self._store.put(key, gzip_if_smaller(body))

    # ------------------------------------------------------------------
    # Client-facing output download (presigned GET per .gz part)
    # ------------------------------------------------------------------

    async def list_output_keys(self, app: str, job_id: str) -> list[str]:
        """All output-part object keys for a job (each is a gzipped ``.jsonl.gz`` part)."""
        return sorted(await self._store.list_keys(f"{job_prefix(app, job_id)}/output/"))

    async def signed_get_output(self, key: str, *, expires_s: int) -> str:
        """Presigned GET for one output part so a client downloads the .gz directly (no creds, no
        proxying MBs through the gateway)."""
        return await self._store.signed_url(key, method="GET", expires_s=expires_s)
