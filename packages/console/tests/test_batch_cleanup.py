"""Tests for the abandoned batch-upload cleanup sweeper."""

from __future__ import annotations

import pytest
from sluice_core.batch_models import BatchManifest
from sluice_core.batch_paths import input_key, job_prefix, manifest_key
from sluice_core.errors import KeyNotFound
from sluice_core.testing.fakes import FakeObjectStore


async def _seed_manifest(store: FakeObjectStore, manifest: BatchManifest) -> None:
    key = manifest_key(manifest.app, manifest.job_id)
    await store.put(key, manifest.model_dump_json().encode())


async def _seed_input(store: FakeObjectStore, app: str, job_id: str, filename: str) -> None:
    await store.put(input_key(app, job_id, filename), b"data")


@pytest.mark.asyncio
async def test_should_delete_stale_job_and_return_prefix_when_state_is_pending_upload_and_past_ttl() -> None:
    """A pending_upload job older than ttl_hours is fully deleted; its prefix is returned."""
    store = FakeObjectStore()
    now = 1_000_000.0
    ttl_hours = 24
    stale_created_at = now - (ttl_hours * 3600 + 1)  # 1 second past TTL

    stale_manifest = BatchManifest(
        job_id="job-stale",
        app="myapp",
        state="pending_upload",
        files=["a.jsonl", "b.jsonl"],
        created_at=stale_created_at,
        sla_hours=4,
    )
    await _seed_manifest(store, stale_manifest)
    await _seed_input(store, "myapp", "job-stale", "a.jsonl")
    await _seed_input(store, "myapp", "job-stale", "b.jsonl")

    # Fresh running job within the TTL — must not be touched.
    fresh_manifest = BatchManifest(
        job_id="job-fresh",
        app="myapp",
        state="running",
        files=["c.jsonl"],
        created_at=now - 3600,  # 1 hour ago — well within 24-hour TTL
        sla_hours=4,
    )
    await _seed_manifest(store, fresh_manifest)
    await _seed_input(store, "myapp", "job-fresh", "c.jsonl")

    from sluice_console.batch_cleanup import sweep

    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    stale_prefix = job_prefix("myapp", "job-stale")
    assert stale_prefix in deleted, f"Expected stale prefix in deleted list, got: {deleted}"

    # All stale objects are gone.
    remaining = await store.list_keys("AppData/")
    for key in remaining:
        assert "job-stale" not in key, f"Stale key still present: {key}"

    # Fresh job is untouched.
    assert manifest_key("myapp", "job-fresh") in remaining
    assert input_key("myapp", "job-fresh", "c.jsonl") in remaining


@pytest.mark.asyncio
async def test_should_delete_stale_running_job_when_past_ttl() -> None:
    """A running job older than ttl_hours is swept the same as pending_upload."""
    store = FakeObjectStore()
    now = 2_000_000.0
    ttl_hours = 2

    stale_running = BatchManifest(
        job_id="job-stale-run",
        app="otherapp",
        state="running",
        files=["x.jsonl"],
        created_at=now - (ttl_hours * 3600 + 60),
        sla_hours=1,
    )
    await _seed_manifest(store, stale_running)
    await _seed_input(store, "otherapp", "job-stale-run", "x.jsonl")

    from sluice_console.batch_cleanup import sweep

    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert job_prefix("otherapp", "job-stale-run") in deleted
    remaining = await store.list_keys("AppData/")
    assert remaining == []


@pytest.mark.asyncio
async def test_should_leave_completed_job_untouched_when_past_ttl() -> None:
    """Completed jobs are never swept regardless of age."""
    store = FakeObjectStore()
    now = 3_000_000.0
    ttl_hours = 1

    old_completed = BatchManifest(
        job_id="job-done",
        app="app1",
        state="completed",
        files=["f.jsonl"],
        created_at=now - (ttl_hours * 3600 + 999),
        sla_hours=2,
    )
    await _seed_manifest(store, old_completed)
    await _seed_input(store, "app1", "job-done", "f.jsonl")

    from sluice_console.batch_cleanup import sweep

    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert deleted == []
    remaining = await store.list_keys("AppData/")
    assert len(remaining) == 2  # manifest + input


@pytest.mark.asyncio
async def test_should_leave_fresh_pending_upload_job_untouched_when_within_ttl() -> None:
    """A pending_upload job within the TTL window is not swept."""
    store = FakeObjectStore()
    now = 4_000_000.0
    ttl_hours = 24

    fresh = BatchManifest(
        job_id="job-new",
        app="app2",
        state="pending_upload",
        files=["g.jsonl"],
        created_at=now - 60,  # 1 minute ago
        sla_hours=4,
    )
    await _seed_manifest(store, fresh)
    await _seed_input(store, "app2", "job-new", "g.jsonl")

    from sluice_console.batch_cleanup import sweep

    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["partial", "failed"])
async def test_should_leave_terminal_state_job_untouched_when_past_ttl(terminal_state: str) -> None:
    """Jobs in partial or failed state are never swept regardless of age."""
    store = FakeObjectStore()
    now = 5_000_000.0
    ttl_hours = 1

    old_terminal = BatchManifest(
        job_id="job-terminal",
        app="app3",
        state=terminal_state,
        files=["h.jsonl"],
        created_at=now - (ttl_hours * 3600 + 999),
        sla_hours=2,
    )
    await _seed_manifest(store, old_terminal)
    await _seed_input(store, "app3", "job-terminal", "h.jsonl")

    from sluice_console.batch_cleanup import sweep

    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert deleted == [], f"Expected no deletions for state={terminal_state!r}, got: {deleted}"
    remaining = await store.list_keys("AppData/")
    assert len(remaining) == 2  # manifest + input


@pytest.mark.asyncio
async def test_should_skip_manifest_when_unparseable() -> None:
    """A job whose manifest.json is corrupt bytes is skipped — no deletion, no exception."""
    store = FakeObjectStore()
    now = 6_000_000.0
    ttl_hours = 1

    # Seed a corrupt manifest directly (bypasses _seed_manifest which writes valid JSON).
    corrupt_key = manifest_key("app4", "job-corrupt")
    await store.put(corrupt_key, b"not json")
    # Seed an input file so we can confirm it is untouched.
    await _seed_input(store, "app4", "job-corrupt", "i.jsonl")

    from sluice_console.batch_cleanup import sweep

    # Must not raise, must not delete anything.
    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert deleted == [], f"Expected no deletions for corrupt manifest, got: {deleted}"
    remaining = await store.list_keys("AppData/")
    assert corrupt_key in remaining
    assert input_key("app4", "job-corrupt", "i.jsonl") in remaining


@pytest.mark.asyncio
async def test_should_continue_sweep_when_key_vanishes_between_list_and_delete() -> None:
    """A key that disappears between list_keys and delete does not abort the sweep."""

    class _RaiseyOnDelete(FakeObjectStore):
        """Raises KeyNotFound on the first delete call to simulate a benign race."""

        def __init__(self) -> None:
            super().__init__()
            self._delete_calls: int = 0

        async def delete(self, key: str) -> None:
            self._delete_calls += 1
            if self._delete_calls == 1:
                raise KeyNotFound(key)
            await super().delete(key)

    store = _RaiseyOnDelete()
    now = 7_000_000.0
    ttl_hours = 1

    stale = BatchManifest(
        job_id="job-race",
        app="app5",
        state="pending_upload",
        files=["j.jsonl"],
        created_at=now - (ttl_hours * 3600 + 1),
        sla_hours=2,
    )
    await _seed_manifest(store, stale)
    await _seed_input(store, "app5", "job-race", "j.jsonl")

    from sluice_console.batch_cleanup import sweep

    # Must not raise even though first delete raises KeyNotFound.
    deleted = await sweep(store=store, now=now, ttl_hours=ttl_hours)

    assert job_prefix("app5", "job-race") in deleted
