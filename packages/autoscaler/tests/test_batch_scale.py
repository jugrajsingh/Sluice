from __future__ import annotations

from sluice_autoscaler.batch_scale import sla_desired


def test_should_fire_second_vm_when_infer_exceeds_sla_window() -> None:
    # rate 1000/min, SLA 30 min -> one VM clears exactly 30 000 within the window
    assert (
        sla_desired(
            infer_visible=30000,
            batch_remaining=0,
            rate_per_min=1000,
            infer_sla_min=30,
            batch_sla_hr=24,
            max_instances=8,
        )
        == 1
    )
    assert (
        sla_desired(
            infer_visible=30001,
            batch_remaining=0,
            rate_per_min=1000,
            infer_sla_min=30,
            batch_sla_hr=24,
            max_instances=8,
        )
        == 2
    )


def test_should_need_one_vm_when_batch_fits_24h_window() -> None:
    # 10 000 batch items, 1000/min rate, 24 h window -> batch_need = ceil(10000/1440000) = 1
    assert (
        sla_desired(
            infer_visible=0,
            batch_remaining=10000,
            rate_per_min=1000,
            infer_sla_min=30,
            batch_sla_hr=24,
            max_instances=8,
        )
        == 1
    )


def test_should_cap_desired_at_max_instances_when_demand_exceeds_ceiling() -> None:
    # 10^9 infer items far exceeds any reasonable instance count; result must be capped at max_instances
    assert (
        sla_desired(
            infer_visible=10**9,
            batch_remaining=0,
            rate_per_min=1000,
            infer_sla_min=30,
            batch_sla_hr=24,
            max_instances=8,
        )
        == 8
    )


def test_should_not_cap_when_max_instances_is_zero_unbounded() -> None:
    # max_instances=0 means UNBOUNDED: return the raw max(infer_need, batch_need) with no ceiling.
    # 90_001 infer items / 30_000 per-instance capacity = ceil = 4 (would clamp to 8 if capped, 0 = no cap)
    assert (
        sla_desired(
            infer_visible=90_001,
            batch_remaining=0,
            rate_per_min=1000,
            infer_sla_min=30,
            batch_sla_hr=24,
            max_instances=0,
        )
        == 4
    )
