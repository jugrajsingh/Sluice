"""Expand an app's ordered placement list into concrete, keyable candidates.

`AppSpec.placement` is an ordered array of typed candidates (kubernetes | vm); the list
order IS the priority. `expand_candidates` flattens it into one `Candidate` per concrete
attempt — a k8s candidate fans out to one per node-selector (ordered fallback), a vm
candidate to one per region — preserving the author's order. `candidate_key` gives each
attempt a stable identity (per cluster/location/selector/gpu/pricing) so stockouts are
shared across apps without one pool's exhaustion suppressing another.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field
from sluice_core.models import AppSpec, Toleration


class Candidate(BaseModel):
    type: Literal["kubernetes", "vm"]
    pricing: Literal["spot", "on-demand"]
    cluster: str  # k8s: provider (in-cluster | <registered name>); vm: provider (gce | ec2)
    location: str = ""  # vm region; k8s zones are encoded in the selector instead
    gpu_type: str = ""
    selector: dict[str, str] = Field(default_factory=dict)  # k8s node selector for this attempt
    tolerations: list[Toleration] = Field(default_factory=list)  # k8s only
    schedule_grace_s: int = 180  # k8s Pending grace before this candidate is stocked out


def _selector_hash(selector: dict[str, str]) -> str:
    if not selector:
        return "none"
    blob = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    return hashlib.sha1(blob.encode(), usedforsecurity=False).hexdigest()[:8]


def candidate_key(c: Candidate) -> str:
    return f"{c.type}/{c.cluster}/{c.location or 'any'}/{_selector_hash(c.selector)}/{c.gpu_type or 'none'}/{c.pricing}"


def expand_candidates(app: AppSpec) -> list[Candidate]:
    out: list[Candidate] = []
    gpu = app.resources.gpu_type
    for cand in app.placement:
        if cand.type == "kubernetes":
            spec = cand.spec
            for selector in spec.node_selectors or [{}]:
                out.append(
                    Candidate(
                        type="kubernetes",
                        pricing=spec.pricing,
                        cluster=cand.provider,
                        gpu_type=gpu,
                        selector=dict(selector),
                        tolerations=list(spec.tolerations),
                        schedule_grace_s=spec.schedule_grace_s,
                    )
                )
        else:  # vm
            for region in cand.spec.regions:
                out.append(
                    Candidate(
                        type="vm",
                        pricing=cand.spec.pricing,
                        cluster=cand.provider,
                        location=region,
                        gpu_type=gpu,
                    )
                )
    return out
