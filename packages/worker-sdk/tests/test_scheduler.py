from __future__ import annotations

from sluice_worker.scheduler import DualSourceScheduler


def _s(now: float = 0.0) -> tuple[DualSourceScheduler, dict[str, float]]:
    clock: dict[str, float] = {"t": now}
    s = DualSourceScheduler(put_concurrency=8, starve_grace_s=420, now=lambda: clock["t"])
    return s, clock


def test_should_prefer_infer_when_infer_available() -> None:
    s, _ = _s()
    assert s.next_source(infer_available=True, batch_available=True, batch_idle_since=None) == "infer"


def test_should_backfill_batch_when_no_infer() -> None:
    s, _ = _s()
    assert s.next_source(infer_available=False, batch_available=True, batch_idle_since=None) == "batch"


def test_should_return_none_when_no_work() -> None:
    s, _ = _s()
    assert s.next_source(infer_available=False, batch_available=False, batch_idle_since=None) == "none"


def test_should_release_slot_to_batch_when_starved_past_grace() -> None:
    s, clock = _s(now=1000.0)
    # batch idle (starved) since t=0, now well past grace, infer still available
    assert s.next_source(infer_available=True, batch_available=True, batch_idle_since=0.0) == "batch"
    # within grace -> still full-yield to infer
    assert s.next_source(infer_available=True, batch_available=True, batch_idle_since=999.0) == "infer"


def test_should_note_infer_present_stores_flag() -> None:
    s, _ = _s()
    s.note_infer_present(True)
    assert s.infer_present is True
    s.note_infer_present(False)
    assert s.infer_present is False


def test_should_reserve_for_batch_when_starvation_condition_met() -> None:
    # put_concurrency=8, starve_grace_s=420, clock at t=500
    # batch_idle_since=0 → now()-idle = 500 >= 420 → reserve=True when in_flight_infer < 8
    s, _ = _s(now=500.0)
    assert s.reserve_for_batch(in_flight_infer=7, batch_idle_since=0.0) is True


def test_should_not_reserve_for_batch_when_within_grace() -> None:
    # batch has not been idle long enough — no starvation condition
    # idle since t=0, elapsed=100 < 420 grace → no reserve
    s, _ = _s(now=100.0)
    assert s.reserve_for_batch(in_flight_infer=7, batch_idle_since=0.0) is False


def test_should_not_reserve_for_batch_when_batch_no_longer_idle() -> None:
    # Regression: old sticky code kept _last_batch_idle_since set after batch_idle_since=0.0
    # was passed to next_source, so a later call with batch_idle_since=None would still return True.
    # The fix makes reserve_for_batch stateless — it uses the passed-in batch_idle_since directly.
    s, clock = _s(now=1000.0)
    # batch was starved: next_source fires the starvation floor
    assert s.next_source(infer_available=True, batch_available=True, batch_idle_since=0.0) == "batch"
    # batch has now resumed — caller passes batch_idle_since=None to reserve_for_batch
    # must return False (no starvation active), regardless of prior next_source call
    assert s.reserve_for_batch(in_flight_infer=0, batch_idle_since=None) is False
