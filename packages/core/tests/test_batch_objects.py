from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from sluice_core.batch_models import BatchFileStatus, BatchManifest
from sluice_core.batch_objects import BatchObjects
from sluice_core.testing.fakes import FakeObjectStore


@pytest.mark.asyncio
async def test_should_round_trip_manifest_and_aggregate_status_when_two_files_have_different_states():
    bo = BatchObjects(store=FakeObjectStore())
    await bo.put_manifest(
        BatchManifest(
            job_id="J1",
            app="sam3",
            state="running",
            files=["a.jsonl", "b.jsonl"],
            created_at=time.time(),
            sla_hours=24,
        )
    )
    await bo.put_file_status(
        "sam3",
        "J1",
        BatchFileStatus(file="a.jsonl", state="completed", records_total=1000, records_done=1000),
    )
    await bo.put_file_status(
        "sam3",
        "J1",
        BatchFileStatus(file="b.jsonl", state="running", records_total=1000, records_done=400),
    )
    agg = await bo.aggregate_status("sam3", "J1")
    assert agg.files_total == 2 and agg.files_done == 1 and agg.files_running == 1
    assert agg.records_done == 1400 and agg.records_total == 2000
    assert agg.output_prefix == "AppData/sam3/batch/J1/output"


@pytest.mark.asyncio
async def test_should_count_unstarted_file_as_pending_when_manifest_has_no_status_object():
    """Manifest has 3 files; only 1 has a status object (running). The other 2 are unstarted."""
    bo = BatchObjects(store=FakeObjectStore())
    await bo.put_manifest(
        BatchManifest(
            job_id="J2",
            app="sam3",
            state="running",
            files=["a.jsonl", "b.jsonl", "c.jsonl"],
            created_at=time.time(),
            sla_hours=24,
        )
    )
    # Only one file has a status object written — the other two have not been picked up yet.
    await bo.put_file_status(
        "sam3",
        "J2",
        BatchFileStatus(file="a.jsonl", state="running", records_total=500, records_done=100),
    )
    agg = await bo.aggregate_status("sam3", "J2")
    assert agg.files_total == 3
    assert agg.files_running == 1
    assert agg.files_pending == 2
    assert agg.files_done == 0
    # Invariant: done + running + pending == total
    assert agg.files_done + agg.files_running + agg.files_pending == agg.files_total


@pytest.mark.asyncio
async def test_should_propagate_when_get_manifest_raises_unexpected_error():
    """A store I/O error (not KeyNotFound) must propagate, not be swallowed."""
    bo = BatchObjects(store=FakeObjectStore())
    boom = RuntimeError("store I/O failure")
    with patch.object(bo, "get_manifest", new=AsyncMock(side_effect=boom)):
        with pytest.raises(RuntimeError, match="store I/O failure"):
            await bo.aggregate_status("sam3", "J99")


@pytest.mark.asyncio
async def test_should_presign_get_for_input_key_when_signing_batch_input():
    """The broker mints a presigned GET over the file's input_key so a VM worker
    (no direct store creds) can fetch the JSONL body."""
    bo = BatchObjects(store=FakeObjectStore())
    url = await bo.signed_get_input("sam3", "J1", "a.jsonl", expires_s=900)
    # FakeObjectStore renders memory://<method>/<key>?exp=<n>
    assert url == "memory://GET/AppData/sam3/batch/J1/input/a.jsonl?exp=900"


@pytest.mark.asyncio
async def test_should_reject_unsafe_filename_when_signing_batch_input():
    bo = BatchObjects(store=FakeObjectStore())
    with pytest.raises(ValueError, match="filename"):
        await bo.signed_get_input("sam3", "J1", "../escape.jsonl", expires_s=900)


@pytest.mark.asyncio
async def test_should_fall_back_to_status_listing_when_manifest_is_absent():
    """KeyNotFound from get_manifest must produce the legacy status-listing fallback."""
    bo = BatchObjects(store=FakeObjectStore())
    # No manifest written — store raises KeyNotFound on get_manifest.
    await bo.put_file_status(
        "sam3",
        "J99",
        BatchFileStatus(file="a.jsonl", state="running", records_total=500, records_done=100),
    )
    agg = await bo.aggregate_status("sam3", "J99")
    assert agg.files_total == 1
    assert agg.files_running == 1


@pytest.mark.asyncio
async def test_should_presign_put_for_output_part_key_when_signing():
    bo = BatchObjects(store=FakeObjectStore())
    url = await bo.signed_put_output_part("sam3", "J1", "a.jsonl", 50, expires_s=900)
    # FakeObjectStore renders memory://<method>/<key>?exp=<n>; the worker streams its part to this PUT.
    assert url == "memory://PUT/AppData/sam3/batch/J1/output/a.jsonl.part-000000050.jsonl.gz?exp=900"
