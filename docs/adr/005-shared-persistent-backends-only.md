# ADR-005: Deployable backends are shared and persistent only

**Date**: 2026-06-13
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (gateway broker), ADR-003 (object-store spec store)

## Context

Sluice is multi-process: the gateway (scaled) and the autoscaler (leader-elected
singleton) run as separate pods, and workers run as separate pods or VMs. Two
in-process backends existed as selectable options — an in-memory `Queue` and a
filesystem `ObjectStore` (plus an in-memory `Cache`):

- An **in-memory queue** can't be shared between the gateway (enqueues) and the
  worker path — each process has its own. A **local filesystem** store likewise isn't
  visible across pods, and the spec store (object store) would not be shared between
  console/CLI (writers) and gateway/autoscaler (readers), so nothing would scale.
- The **stockout board** lives in the `Cache`. In-memory, a leader failover loses it,
  and the autoscaler re-pays failed placements (stuck pods) to relearn it — discarding
  the cross-app sharing that is the board's whole point.

## Decision

Only shared, persistent backends are selectable in the factory, config, and chart:

| Interface | Allowed backends |
|---|---|
| Queue | `redis`, `sqs` |
| ObjectStore | `s3`, `minio`, `gcs` |
| Cache | `redis`, `objectstore` |
| AppRegistry | `objectstore` |

`local`/`memory` are removed from the driver factory entirely (an invalid backend
raises at startup). Default cache is `objectstore` — it reuses the bucket every
deployment already has, so the stockout board persists across autoscaler restarts with
no extra dependency.

Because every object store now signs URLs, **signing is mandatory**: the gateway broker
always hands workers pre-signed URLs (no fallback), and the worker JWT is sent only to
the gateway, never to object storage (ADR-001/002).

## Consequences

- A deployment cannot silently pick a backend that breaks multi-process operation.
- The control plane is fully shared-state; leader failover keeps the stockout board.
- Tests use the real drivers + emulators (moto/fakeredis) for driver conformance, and a
  single in-process test double (`sluice_core.testing.fakes`) — **not** a deployable
  backend — for component logic. The double is conformance-locked: it must pass the same
  `Queue`/`ObjectStore` suites as the real drivers, so it can never drift from the
  contract.
- Self-contained (no-cloud) deployments need a bundled or external Redis + S3-compatible
  store (e.g. MinIO). Bundling is tracked with the multi-cluster placement work.
