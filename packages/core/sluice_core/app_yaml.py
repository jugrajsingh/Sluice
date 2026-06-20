from __future__ import annotations

import yaml
from pydantic import ValidationError

from .models import AppSpec


def parse_app_yaml(text: str) -> AppSpec:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or doc.get("apiVersion") != "sluice/v1" or doc.get("kind") != "App":
        raise ValueError("expected a document with apiVersion: sluice/v1, kind: App")
    name = (doc.get("metadata") or {}).get("name")
    if not name:
        raise ValueError("metadata.name is required")
    spec = dict(doc.get("spec") or {})
    data: dict = {"name": name}
    if "queue" in spec:
        q = spec.pop("queue") or {}
        extra = set(q) - {"ref"}
        if extra:
            raise ValueError(f"queue: unknown key(s) {sorted(extra)} (only 'ref' is allowed)")
        data["queueRef"] = q.get("ref", "")
    if "storage" in spec:
        s = spec.pop("storage") or {}
        extra = set(s) - {"prefix"}
        if extra:
            raise ValueError(f"storage: unknown key(s) {sorted(extra)} (only 'prefix' is allowed)")
        data["storagePrefix"] = s.get("prefix", "")
    data.update(spec)
    try:
        return AppSpec.model_validate(data)
    except ValidationError:
        # Re-raise the structured form so callers (e.g. the CLI) can render friendly,
        # per-field messages. ValidationError subclasses ValueError, so ValueError
        # callers still catch it.
        raise
    except Exception as e:  # any other error -> ValueError for callers
        raise ValueError(str(e)) from e


def serialize_app_yaml(app: AppSpec) -> str:
    spec = app.model_dump(by_alias=True, exclude={"name"})
    spec["queue"] = {"ref": spec.pop("queueRef")}
    spec["storage"] = {"prefix": spec.pop("storagePrefix")}
    return yaml.safe_dump(
        {"apiVersion": "sluice/v1", "kind": "App", "metadata": {"name": app.name}, "spec": spec}, sort_keys=False
    )
