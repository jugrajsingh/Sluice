from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sluice_core.models import AppSpec


class Candidate(BaseModel):
    substrate: Literal["kubernetes", "vm"]
    pricing: Literal["spot", "on-demand"]
    provider: str  # "k8s" | "gce" | "ec2"
    location: str = ""  # zone (k8s; "" = any) or region (vm)
    gpu_type: str = ""
    selector: dict[str, str] = Field(default_factory=dict)  # k8s only


def candidate_key(c: Candidate) -> str:
    return f"{c.substrate}/{c.provider}/{c.location or 'any'}/{c.gpu_type or 'none'}/{c.pricing}"


def expand_candidates(app: AppSpec) -> list[Candidate]:
    out: list[Candidate] = []
    p = app.placement
    for pricing in p.pricing:
        if p.mode in ("kubernetes", "both"):
            for pool in p.kubernetes:
                if pool.pricing != pricing:
                    continue
                for zone in pool.zones or [""]:
                    selector = dict(pool.selector)
                    if zone:
                        selector["topology.kubernetes.io/zone"] = zone
                    out.append(
                        Candidate(
                            substrate="kubernetes",
                            pricing=pricing,
                            provider="k8s",
                            location=zone,
                            gpu_type=app.resources.gpu_type,
                            selector=selector,
                        )
                    )
        if p.mode in ("vm", "both") and p.vm is not None:
            for region in p.vm.regions:
                out.append(
                    Candidate(
                        substrate="vm",
                        pricing=pricing,
                        provider=p.vm.provider,
                        location=region,
                        gpu_type=app.resources.gpu_type,
                    )
                )
    return out
