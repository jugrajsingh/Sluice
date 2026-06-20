from __future__ import annotations

import math


def sla_desired(
    *,
    infer_visible: int,
    batch_remaining: int,
    rate_per_min: int,
    infer_sla_min: int,
    batch_sla_hr: int,
    max_instances: int,
) -> int:
    """Return the instance count required to meet both infer and batch SLA windows.

    Args:
        infer_visible: Number of infer-queue messages currently visible.
        batch_remaining: Number of batch items not yet processed.
        rate_per_min: Throughput of a single instance in items per minute.
        infer_sla_min: Maximum acceptable infer latency in minutes.
        batch_sla_hr: Maximum acceptable batch completion time in hours.
        max_instances: Hard ceiling on the number of instances to return; ``0`` means UNBOUNDED.

    Returns:
        ``min(cap, max(infer_need, batch_need))`` where each need is the ceiling of the required
        throughput divided by the capacity one instance can provide within the SLA window, and
        ``cap`` is ``max_instances`` when positive else the raw max (no ceiling). Both denominators
        are floored at 1 so we never divide by zero. Needs are floored at 0 so we never return
        negative values.
    """
    infer_capacity = max(1, rate_per_min * infer_sla_min)
    batch_capacity = max(1, rate_per_min * batch_sla_hr * 60)

    infer_need = max(0, math.ceil(infer_visible / infer_capacity))
    batch_need = max(0, math.ceil(batch_remaining / batch_capacity))

    cap = max_instances if max_instances > 0 else max(infer_need, batch_need)
    return min(cap, max(infer_need, batch_need))
