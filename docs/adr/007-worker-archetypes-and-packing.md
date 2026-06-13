# ADR-007: Worker archetypes and GPU packing

**Date**: 2026-06-14
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (gateway broker), ADR-002 (short-lived worker creds), ADR-006 (ordered placement)

## Context

A worker ran the model **in-process** as one `BaseHandler`, one pod = one worker = one whole GPU,
with a batch-then-wait loop. Real GPU utilization comes from **packing N model replicas on one
GPU** (no MPS) — the pattern a SamServe-style server uses (`SERVER__WORKERS=3`, N uvicorn processes,
each its own model copy, GPU single-threaded per process; sequential load via a file lock to avoid
host-RAM OOM). Sluice could do this on burst VMs (a fixed `workersPerVm`) but **not on Kubernetes**,
and only for the in-process handler — not for an existing HTTP model server. We also needed the
packing count to vary per placement candidate (an L4 packs 3, an L40S packs 6) and all model env
(`MODEL__*`, `HF_HUB_OFFLINE`, …) to reach workers on both substrates (a real bug dropped it on VMs).

## Options Considered

- **MPS / time-slicing only** (multiple `gpu:1` pods sharing a physical GPU): rejected as the
  default — VRAM overhead and depends on cluster-side GPU-sharing config; kept as an orthogonal
  option the operator can still use.
- **Dynamic VM bin-packing** (agent probes VRAM/CPU/RAM and computes how many fit): rejected —
  a controller↔agent capacity protocol and a `vram` model field for marginal benefit. The operator
  declares the count offline instead (matching how SamServe is tuned).
- **Multi-tenant VMs** (different apps packed on one VM): rejected — breaks the VM↔app 1:1 and the
  app-scoped worker token; a separate epic.

## Decision

Two worker archetypes, selected by `worker.type`, both packing a user-declared `instances` count
(per-candidate overridable) and counted as **one unit** by the autoscaler:

- **handler**: one launcher (`sluice_worker.launch --instances N`) starts N `worker.run` processes
  **sequentially** — each loads its own model copy; ordering (not an `fcntl` lock) bounds peak host
  RAM to a single load. Each process leases independently.
- **sidecar**: an HTTP model server (its own entrypoint, packs itself, owns the GPU, holds **no**
  credentials, startup-probed) plus a Sluice **adapter** that holds the JWT and feeds it. The
  adapter is **model-agnostic, verbatim passthrough**: the queue body is POSTed to the server as-is
  and the response is stored as the result.

Supporting decisions:

- **Keep-busy dispatch**: maintain ~`instances` in-flight, replenishing on each completion, instead
  of batch-then-wait — keeps the packed GPU saturated.
- **Unit counting**: 1 pod = 1 VM = 1 unit; `messagesPerWorker` is tuned per **packed unit**. This
  removes the per-VM `workersPerVm` multiplier from the capacity math.
- **Cold start**: a Running container whose startup probe hasn't passed (`started=false`) maps to
  `starting`, not `unhealthy` — so a multi-minute model load counts as live and is never stocked out.
- **Full env both substrates**: the autoscaler hands the VM agent the complete worker env
  (`SLUICE_WORKER_ENV`), fixing the prefix-filter that dropped `MODEL__*`/`HF_*`; a docker `args`
  field applies on both.

## Consequences

- The SamServe model — pack N replicas on one GPU, no MPS — works on **both** Kubernetes and burst
  VMs, with the count declared per candidate for heterogeneous GPUs.
- Bring an unmodified HTTP server (sidecar) or a Sluice `BaseHandler` image (handler); the broker /
  short-lived-credential model (ADR-001/002) is unchanged — only the adapter/launcher holds the JWT.
- Dynamic VRAM bin-packing and multi-tenant VMs are explicitly out of scope; `instances` is declared.
- A reference wrapping SamServe as both archetypes lives in `examples/samserve/` (gitignored).
