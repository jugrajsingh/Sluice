"""Pydantic models for the bulk-batch inference feature.

Each file in a batch job has its own status object, written by a single VM worker
that owns that file — no concurrent writers, so no CAS is needed.
"""

from __future__ import annotations

from pydantic import BaseModel


class BatchFileStatus(BaseModel):
    """Per-file progress written by the VM that owns the file."""

    file: str
    state: str  # pending_upload | running | completed | partial | failed
    records_total: int = 0
    records_done: int = 0
    records_failed: int = 0
    updated_at: float = 0.0


class BatchManifest(BaseModel):
    """Job-level manifest created at submission time."""

    job_id: str
    app: str
    state: str  # pending_upload | running | completed | partial | failed
    files: list[str]
    created_at: float
    sla_hours: int


class BatchJobAggregate(BaseModel):
    """Derived aggregate computed by reading all per-file status objects."""

    state: str  # pending_upload | running | completed | partial | failed
    files_total: int
    files_done: int
    files_running: int
    files_pending: int
    records_done: int
    records_total: int
    records_failed: int
    output_prefix: str
