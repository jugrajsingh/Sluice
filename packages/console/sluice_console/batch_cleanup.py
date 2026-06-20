"""Abandoned batch-upload cleanup sweeper.

Deletes batch jobs that are stuck in `pending_upload` or `running` state
past their configured TTL, reclaiming object-store space.

The `sweep()` function is a pure-ish async function operating over an
`ObjectStore`; the `main()` entrypoint wires it to real infrastructure.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError
from sluice_core.batch_models import BatchManifest
from sluice_core.batch_paths import job_prefix
from sluice_core.errors import KeyNotFound
from sluice_core.interfaces import ObjectStore

logger = logging.getLogger(__name__)

_SWEEPABLE_STATES = frozenset({"pending_upload", "running"})

# Broad prefix that covers all app batch namespaces: AppData/<app>/batch/<job>/...
_BATCH_INFIX = "/batch/"
_MANIFEST_SUFFIX = "/manifest.json"


async def sweep(
    *,
    store: ObjectStore,
    now: float,
    ttl_hours: int,
    root: str = "AppData",
) -> list[str]:
    """Scan all batch manifests under `root` and delete stale jobs.

    A job is stale when:
    - its ``state`` is in {``pending_upload``, ``running``}, AND
    - ``now - manifest.created_at > ttl_hours * 3600``

    Jobs in ``completed``, ``partial``, or ``failed`` state are never touched,
    regardless of age.

    Args:
        store:     Object store to scan and delete from.
        now:       Current epoch-seconds timestamp (injectable for testability).
        ttl_hours: Maximum age in hours for sweepable-state jobs before deletion.
        root:      Top-level prefix to scan. Defaults to ``"AppData"``.

    Returns:
        List of job prefixes that were deleted (e.g. ``["AppData/myapp/batch/job-id"]``).
    """
    ttl_seconds = ttl_hours * 3600
    deleted_prefixes: list[str] = []

    all_keys = await store.list_keys(f"{root}/")
    manifest_keys = [k for k in all_keys if _BATCH_INFIX in k and k.endswith(_MANIFEST_SUFFIX)]

    for manifest_key in manifest_keys:
        manifest = await _parse_manifest(store, manifest_key)
        if manifest is None:
            continue

        if manifest.state not in _SWEEPABLE_STATES:
            continue

        age = now - manifest.created_at
        if age <= ttl_seconds:
            continue

        prefix = job_prefix(manifest.app, manifest.job_id)
        await _delete_job(store, prefix)
        deleted_prefixes.append(prefix)
        logger.info(
            "Swept stale batch job",
            extra={
                "app": manifest.app,
                "job_id": manifest.job_id,
                "state": manifest.state,
                "age_hours": age / 3600,
            },
        )

    return deleted_prefixes


async def _parse_manifest(store: ObjectStore, key: str) -> BatchManifest | None:
    """Return parsed BatchManifest or None when absent or unparseable."""
    try:
        raw = await store.get(key)
        return BatchManifest.model_validate_json(raw)
    except (KeyNotFound, ValidationError, ValueError):
        logger.warning("skipping unparseable batch manifest at %s", key)
        return None


async def _delete_job(store: ObjectStore, prefix: str) -> None:
    """Delete every object under the given job prefix."""
    keys = await store.list_keys(f"{prefix}/")
    for key in keys:
        try:
            await store.delete(key)
        except KeyNotFound:
            logger.warning("batch cleanup: key already gone %s", key)


def main() -> None:
    """CLI entrypoint: sweep abandoned batch jobs using store from Settings."""
    import argparse  # noqa: PLC0415
    import time  # noqa: PLC0415

    from sluice_core.config import Settings  # noqa: PLC0415
    from sluice_drivers.factory import build_object_store  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Sweep abandoned batch jobs from the object store.")
    parser.add_argument("--ttl-hours", type=int, default=24)
    args = parser.parse_args()

    settings = Settings()
    store = build_object_store(settings)

    deleted = asyncio.run(sweep(store=store, now=time.time(), ttl_hours=args.ttl_hours))
    logger.info("Batch cleanup complete", extra={"deleted_count": len(deleted)})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
