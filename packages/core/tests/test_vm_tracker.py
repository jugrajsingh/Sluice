from __future__ import annotations

import pytest
from sluice_core.testing.fakes import FakeObjectStore
from sluice_core.vm_tracker import VmTracker

pytestmark = pytest.mark.asyncio


async def test_should_upsert_and_list_per_app_region():
    t = VmTracker(FakeObjectStore())
    await t.upsert("m", "r1", name="sluice-m-aaa", state="provisioning", created_at=100.0)
    await t.upsert("m", "r1", name="sluice-m-bbb", state="running", created_at=101.0)
    names = {e.name for e in await t.entries("m", "r1")}
    assert names == {"sluice-m-aaa", "sluice-m-bbb"}
    # region-scoped: a different (app,region) is an independent ledger
    assert await t.entries("m", "r2") == []
    assert await t.entries("other", "r1") == []


async def test_should_update_existing_entry_on_reupsert():
    t = VmTracker(FakeObjectStore())
    await t.upsert("m", "r1", name="v1", state="provisioning", created_at=1.0)
    await t.upsert("m", "r1", name="v1", state="running", created_at=1.0)
    entries = await t.entries("m", "r1")
    assert len(entries) == 1 and entries[0].state == "running"


async def test_should_mark_gone_removes_entry():
    t = VmTracker(FakeObjectStore())
    await t.upsert("m", "r1", name="v1", state="running", created_at=1.0)
    await t.upsert("m", "r1", name="v2", state="running", created_at=1.0)
    await t.mark_gone("m", "r1", "v1")
    assert {e.name for e in await t.entries("m", "r1")} == {"v2"}


async def test_should_log_error_keeps_entry_and_records_event():
    t = VmTracker(FakeObjectStore())
    await t.upsert("m", "r1", name="v1", state="running", created_at=1.0)
    await t.log_error("m", "r1", "v1", "prober timeout")
    entries = await t.entries("m", "r1")
    assert len(entries) == 1 and entries[0].last_error == "prober timeout"
    assert any("prober timeout" in e.error for e in await t.events("m", "r1"))


async def test_should_log_error_for_untracked_name_without_creating_entry():
    # A provision that never made it into the ledger can still log a failure event.
    t = VmTracker(FakeObjectStore())
    await t.log_error("m", "r1", "ghost", "stockout")
    assert await t.entries("m", "r1") == []
    assert any(e.error == "stockout" for e in await t.events("m", "r1"))


async def test_events_are_capped():
    t = VmTracker(FakeObjectStore())
    for i in range(60):
        await t.log_error("m", "r1", "v1", f"err{i}")
    events = await t.events("m", "r1")
    assert len(events) <= 50  # bounded; keeps the most recent
    assert events[-1].error == "err59"
