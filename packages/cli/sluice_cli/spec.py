from __future__ import annotations

import difflib

from pydantic import ValidationError
from sluice_core.app_yaml import parse_app_yaml
from sluice_core.models import AppSpec


def _nested_models() -> list:
    from sluice_core import models as m

    return [
        m.ResourcesSpec,
        m.ScalingSpec,
        m.WorkerSpec,
        m.ServerSpec,
        m.BatchSpec,
        m.K8sPlacementSpec,
        m.VmPlacementSpec,
        m.KubernetesCandidate,
        m.VmCandidate,
        m.CandidateOverrides,
        m.Toleration,
    ]


def _known_keys() -> set[str]:
    # field names + aliases across the whole spec tree (good enough for suggestions)
    keys: set[str] = set()
    for model in (AppSpec, *_nested_models()):
        for fname, f in model.model_fields.items():
            keys.add(fname)
            if f.alias:
                keys.add(f.alias)
    return keys


def _friendly(err: ValidationError) -> list[str]:
    known = _known_keys()
    out = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"])
        if e["type"] == "extra_forbidden":
            bad = str(e["loc"][-1])
            near = difflib.get_close_matches(bad, known, n=1)
            hint = f" — did you mean '{near[0]}'?" if near else ""
            out.append(f"spec.{loc}: unknown field{hint}")
        else:
            out.append(f"spec.{loc}: {e['msg']}")
    return out


def load_and_validate(text: str) -> tuple[AppSpec | None, list[str]]:
    try:
        return parse_app_yaml(text), []
    except ValidationError as e:
        return None, _friendly(e)
    except ValueError as e:
        # parse_app_yaml raises plain ValueError for its own queue/storage and document-shape
        # errors (ValidationError is handled above). Surface the message verbatim.
        return None, [str(e)]
