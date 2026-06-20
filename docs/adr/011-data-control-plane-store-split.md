# ADR-011: Data / control-plane object-store split

**Date**: 2026-06-18
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-003 (object-store spec store), ADR-008 (capability-scoped credentials), ADR-009 (bulk-batch inference), ADR-010 (portable batch credentials), ADR-012 (stateless burst-VM lifecycle)

## Context

Today a **single object store** holds two kinds of objects with opposite lifecycles:

- **Data plane** — large, ephemeral: per-request request/result bodies, per-job batch input/output.
- **Control / state** — small, durable: the app-spec registry (ADR-003), the object-store cache, and
  VM heartbeat / desired state.

Co-locating them means one set of lifecycle rules and one IAM grant covers both. The data plane wants
an aggressive TTL (results are short-lived); the control plane must **never** be auto-expired
(deleting a spec or VM state is a control-plane outage). And the only grant a worker should ever
touch is data-plane presigned URLs (ADR-002, ADR-010) — yet today that bucket also contains the
registry and VM state.

## Options Considered

- **Keep one bucket, layer prefix-scoped lifecycle rules and IAM (status quo+)** — possible, but
  prefix-scoped TTL and prefix-scoped IAM are brittle and easy to mis-author; a wrong rule can expire
  a spec. Rejected as the durable shape.
- **Two buckets — data vs. control/state (chosen)** — the lifecycle and IAM boundary is the bucket
  boundary, which both clouds enforce natively. Clean and hard to mis-author.

## Decision

Split the object store into two logical buckets:

- **Data bucket** — large, ephemeral, lifecycle-**TTL'd**. Holds inference + batch objects under the
  **`AppData/{app}/…`** prefix (renamed from `apps/` so a bucket reader and a lifecycle rule can target
  data unambiguously). This is the **only** grant a worker ever needs, reached exclusively via presigned
  URLs (ADR-002, ADR-010).
- **Control / state bucket** — small, durable, no TTL: registry specs + status (`sluice/apps/{app}/…`),
  the object-store cache, VM heartbeats, and the per-(app,region) **VM-tracking ledger**
  (`sluice/apps/{app}/vms/{region}/tracking.json`, ADR-012).

There is **no persistent terraform state** — burst VMs use an ephemeral-state, cloud-as-truth lifecycle
(ADR-012), so the former separate `…-tfstate` bucket is **decommissioned**, not carried forward.

A new **optional `state_store` settings block** is introduced. When unset it **defaults to the data
`object_store`**, so existing single-bucket deployments keep working with no config change
(backward compatible). Operators who want the split set `state_store` to the control/state bucket.

## Consequences

- **Clean IAM and lifecycle boundaries.** The data bucket gets an aggressive TTL and worker-facing
  presigned access; the control/state bucket is durable and control-plane-only. No prefix-scoped
  lifecycle gymnastics.
- **The gateway holds credentials for both buckets.** It signs data-plane URLs against the data
  bucket and proxies VM state against the control/state bucket — both control-plane-only, consistent
  with ADR-008's capability-scoped model.
- **Backward compatible.** Single-bucket deployments are unaffected until they opt in by setting
  `state_store`; the chart emits `STATE_STORE__*` only when a state backend is configured.
- **Gone-VM cruft is GC'd, not TTL'd, on the state bucket.** Stale `…/vms/{vm}/heartbeat.json` is deleted
  when the prober reports the instance gone (a live VM rewrites its heartbeat each poll); specs, status,
  and the tracking ledger are never auto-expired.
- **Implemented.** The `state_store` settings block, `build_state_store` factory (None ⇒ inherit), the
  `AppData/` data-prefix rename, the three-service wiring (registry/cache/VM-state → state store;
  inference/batch → data store), and the chart `stateStore` value + `credentials.state` capability all
  landed in the Stage-2 bucket-split work.
