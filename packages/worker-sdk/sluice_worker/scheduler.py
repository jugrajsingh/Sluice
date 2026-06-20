from __future__ import annotations

from collections.abc import Callable
from typing import Literal


class DualSourceScheduler:
    """Pure scheduling logic for choosing between infer and batch work slots.

    Infer work has priority; batch backfills idle concurrency. When batch has
    been starved past ``starve_grace_s`` seconds, one slot is reserved for batch
    so long-running batch jobs still make progress even during sustained infer load.
    """

    def __init__(
        self,
        put_concurrency: int,
        starve_grace_s: float,
        now: Callable[[], float],
    ) -> None:
        self._put_concurrency = put_concurrency
        self._starve_grace_s = starve_grace_s
        self._now = now
        self.infer_present: bool = False

    def note_infer_present(self, present: bool) -> None:
        """Store the latest infer-presence flag (set by the periodic 10–30 s poll)."""
        self.infer_present = present

    def _starvation_active(self, batch_idle_since: float) -> bool:
        return self._now() - batch_idle_since >= self._starve_grace_s

    def next_source(
        self,
        *,
        infer_available: bool,
        batch_available: bool,
        batch_idle_since: float | None,
    ) -> Literal["infer", "batch", "none"]:
        """Decide which source gets the next freed concurrency slot.

        Priority order:
        1. Starvation floor: if batch has been idle >= starve_grace_s and batch is
           available, return "batch" even when infer is also available.
        2. Prefer infer when available.
        3. Fall back to batch when available.
        4. Return "none" when nothing is available.
        """
        if batch_idle_since is not None and batch_available and self._starvation_active(batch_idle_since):
            return "batch"
        if infer_available:
            return "infer"
        if batch_available:
            return "batch"
        return "none"

    def reserve_for_batch(self, *, in_flight_infer: int, batch_idle_since: float | None) -> bool:
        """Return True when the starvation floor is active.

        Stateless: uses the caller-supplied ``batch_idle_since`` directly rather than
        cached state, so it correctly returns False the moment batch resumes (caller
        passes ``batch_idle_since=None``).

        True when batch has been idle long enough (same condition as next_source)
        AND at least one of put_concurrency is still available for batch
        (i.e. in_flight_infer < put_concurrency).
        """
        return (
            batch_idle_since is not None
            and self._starvation_active(batch_idle_since)
            and in_flight_infer < self._put_concurrency
        )
