from __future__ import annotations

import hashlib
import math


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def eta_seconds(*, visible: int, throughput_per_s: float, min_s: int) -> int:
    return max(min_s, math.ceil(visible / max(throughput_per_s, 0.001)))
