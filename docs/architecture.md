# Architecture

## Overview

Sluice is a queue-driven, scale-from-zero GPU inference platform for any Kubernetes,
with burst to VMs on other clouds when the cluster runs out of capacity. A traffic
spike deepens a queue instead of failing; workers own their lifecycle and self-terminate
when the queue drains; the control plane only scales up and reaps. You bring a model and
a handler — the gateway, control plane, and worker SDK do the rest.

The system splits cleanly into a **control plane** (holds all credentials, makes
decisions) and a **data plane** (moves request/result bytes). The spec store, not the
Kubernetes API, is the source of truth for what apps exist.

## System Context

```
                 ┌──────────────── control plane ────────────────┐
   client ──────▶│  gateway  ─────────────┐                       │
  (HTTPS)        │  (broker + client API) │                       │
                 │     │                  ▼                        │
                 │     │            object store  ◀── spec store   │
                 │     │            (bucket)            (AppSpec)   │
                 │     ▼                  ▲                  ▲      │
                 │   queue (Redis/SQS)    │                  │      │
                 │     ▲                  │            autoscaler   │
                 │     │                  │          (reconcile +   │
                 │     │                  │           token issuer) │
                 └─────┼──────────────────┼──────────────────┼─────┘
                       │ lease/ack (JWT)   │ signed GET/PUT    │ creates + mints JWT
                       │                   │                   ▼
                   worker ◀────────────────┘            worker pods / burst VMs
                (pure broker client)                    (k8s, or AWS/GCP/OVH)
```

## Control plane

| Component | Responsibility | Holds |
|---|---|---|
| **gateway** | Client API (infer / status / batch, sync-sugar, retry-after ETAs) **and** the worker broker (lease/extend/ack/nack, signs object URLs). The single communication highway. Horizontally scaled. | master queue + object-store creds |
| **console** | Read-only worker/event views + admin API (apply/delete/pause/resume/drain apps → spec store). | master object-store creds (spec store) |
| **autoscaler** | Leader-elected reconcile loop: reads queue depth + specs, runs the placement playbook, creates/repans workers (pods or VMs), reaps exited ones. Issues each worker's JWT. | master creds + JWT signing key |

## Data plane & contract

Request bodies and results live in the object store; only a request ID rides the queue
(content hash → dedupe + cache). A worker leases an ID from the gateway broker, reads the
body via a pre-signed URL, runs the handler, writes the result via a pre-signed URL, and
acks. Clients submit to the gateway and poll status; the gateway reads results from the
bucket on their behalf.

### Two lanes per app

Each app has **two queues**: an **online lane** (`{app}-infer`, the app's primary
`queueRef`) and a **batch lane** (`{app}-batch`). The online lane carries one request ID
per message for low-latency, single-request inference. The batch lane carries **one
message per JSONL file** — a message is just `{job_id, file}`, and the file (which may hold
millions of records) is fetched from the object store, not the queue. The batch lane leases
with a **long window** so a multi-minute file is not reclaimed mid-process; the worker
extends the lease from its heartbeat as it streams through the file.

Clients drive a batch job through the gateway's client API: `POST /v1/{app}/batch/{job_id}/upload-url`
mints a presigned PUT per input file, `POST .../submit` verifies the uploads landed and
enqueues one batch message per file, `GET .../{job_id}` aggregates per-file status, and
`GET .../output` mints presigned GETs for the completed output parts.

### Object layout

Storage splits into two logical stores (**ADR-011**): a **data** store (large, ephemeral,
lifecycle-TTL'able) and a **control/state** store (small, durable). They are configured separately
(`object_store` + an optional `state_store`); when `state_store` is unset it **inherits** `object_store`,
so a dev/single-bucket deploy keeps everything in one bucket with no config change.

**Data store** — inference + batch objects, rooted at the app's `storagePrefix` (default `AppData/{app}`):

| Path | Holds |
|---|---|
| `AppData/{app}/requests/{rid}` | raw online request body |
| `AppData/{app}/results/{rid}.gz` | gzipped online result |
| `AppData/{app}/batch/{job_id}/input/{file}` | client-uploaded JSONL input file |
| `AppData/{app}/batch/{job_id}/output/{file}.part-NNNNNNNNN.jsonl.gz` | gzipped output partition (offset-named, resume-safe) |
| `AppData/{app}/batch/{job_id}/status/{file}.json` | per-file progress/checkpoint |
| `AppData/{app}/batch/{job_id}/manifest.json` | job manifest (files + state) |

**Control/state store** — app specs/status, the cache, and VM state (durable; never TTL'd), under the `sluice/` root:

| Path | Holds |
|---|---|
| `sluice/apps/{app}/spec.yaml`, `status.json` | app registry spec + last reconcile status |
| `sluice/apps/{app}/vms/{region}/tracking.json` | per-(app,region) VM tracking ledger (IDs, states, heartbeat timestamps) |
| `sluice/apps/{app}/vms/{vm}/heartbeat.json` | burst-VM heartbeat (worker → control plane); gateway stamps `received_at` |
| `sluice/apps/{app}/vms/{vm}/desired.json` | burst-VM command (control plane → worker) |

Results and batch output parts are gzip-compressed; the trailing `.gz` suffix is the only
signal a bucket reader (or a client's presigned download) needs to inflate — no out-of-band
metadata. There is **no terraform-state bucket** — burst VMs use an ephemeral-state, cloud-as-truth
lifecycle (**ADR-012**); a per-(app,region) VM-tracking ledger lives in the control/state store.

### Worker credentials (cross-cloud)

A worker — whether a Kubernetes pod or a burst VM on another cloud — holds **no backend
credentials**. It carries only a short-lived, app-scoped JWT and reaches everything through
the gateway broker:

- **Queue** via the broker lease endpoints — never the queue itself.
- **Large objects** via broker-minted pre-signed URLs: it requests a GET to read an input
  (online request or batch file) and a PUT to write a result or output part.
- **Small JSON** via broker-proxied calls — the broker performs the tiny store read/write
  on the worker's behalf (batch status, VM heartbeat, VM command), so the worker needs no
  signing path for these.

Because all backend access is brokered and the worker holds no storage/queue keys, the same
app image runs **cross-cloud** unchanged — a pod against an S3 bucket and a burst VM against
a GCS bucket behave identically. Worker coordination and credentials are defined in
**ADR-001** (gateway broker; queue never exposed), **ADR-002** (short-lived app-scoped JWT +
pre-signed URLs), **ADR-008** (capability-scoped, mounted control-plane credentials — never
Workload Identity), and **ADR-010** (the worker credential model). The broker endpoints:

| Endpoint | Lane | Purpose |
|---|---|---|
| `/internal/v1/lease` · `/ack` · `/extend` · `/nack` | online | lease an `{app}-infer` ID, ack/extend/nack it |
| `/internal/v1/batch/lease` · `/ack` · `/extend` · `/nack` | batch | lease one `{app}-batch` file (long window), ack/extend/nack |
| `/internal/v1/batch/output-url` | batch | presigned PUT for an output part |
| `/internal/v1/batch/status` (GET/POST) | batch | broker-proxied per-file status read/write |
| `/internal/v1/vm/heartbeat` · `/internal/v1/vm/command` | VM | broker-proxied heartbeat PUT and command GET |

## Spec store (source of truth)

Apps are defined by an `AppSpec` (image, handler, queue ref, storage prefix, resources,
scaling, placement) posted via the CLI or admin API and stored in the object store
(`AppRegistry` over `ObjectStore`) — kops-style, so the control plane is stateless and
restartable. There are no CRDs; the Kubernetes API is used only to create/observe bare
worker pods. See **ADR-003** (object-store spec store).

## Scale-from-zero (emergent)

Zero is not a controller decision. Workers exit when the queue is empty; the controller
only scales up and reaps the exited. Idle apps cost nothing, with no Deployment churn.
Burst VMs linger briefly for warm restart, then power off; the controller tears them down.

## On-box dual-source scheduler

One worker unit serves **both lanes**. Inside the worker, a pure scheduler decides which
lane gets each freed model slot:

- **Infer is priority.** A freed model slot goes to online inference whenever online work is
  present, and batch **fully yields** to it — batch runs only on capacity online inference
  is not using, backfilling otherwise-idle slots.
- **Starvation floor.** After `starveGraceMin` (default 7 min) of no batch progress under
  sustained online load, the scheduler reserves at least one slot for batch (≥ 1 of
  `putConcurrency`, i.e. ≥ 1/8 at the default 8), so a 24-hour batch job still drips forward
  instead of stalling indefinitely behind online traffic.
- **One concurrency budget.** A single `putConcurrency` semaphore (default 8) bounds the
  total in-flight model calls **across both lanes** — online and batch draw from the same
  budget, never exceeding it combined.
- **No preemption.** Already-leased records — online or batch — are never preempted; the
  scheduler only governs how *freed* slots are handed out.

## Placement

Each app declares `placement` as an **ordered list of candidates** (`kubernetes` or `vm`); the
list order is the priority. The autoscaler runs a pure playbook that walks the list, and for a
Kubernetes candidate synthesizes a pod with the candidate's **node selector** (an ordered list —
a targeted GPU pool first, a broader label next), GPU **tolerations**, and the `nvidia.com/gpu`
limit, then lets the scheduler place it. A stuck pod is classified from its
`PodScheduled=Unschedulable` message: capacity exhaustion marks the candidate stocked out (after
a per-candidate grace; immediately when the node group is maxed) in a shared cache and advances to
the next candidate, while an untolerated-taint **config bug** is surfaced rather than stocked out.

A k8s candidate's `provider` selects the cluster — `in-cluster` or a registered external cluster
(kubeconfig in `AUTOSCALER__CLUSTERS`) — so one (possibly GPU-less) Sluice orchestrates workers
across **many clusters and clouds**. VM burst is just another candidate, provisioned via Terraform.
See **ADR-006** (multi-cluster ordered node-aware placement), which supersedes the implicit
pricing order of **ADR-004** while keeping its pure plan and shared stockouts.

## Dual-lane SLA scaling

The autoscaler sizes each app against **both lanes at once** and takes the larger demand,
clamped to the app's ceiling:

```
desired = min(maxInstances,
              max(ceil(infer_visible / (rate × inferSlaMinutes)),
                  ceil(batch_remaining / (rate × batchSlaHours × 60))))
```

where `rate` is one unit's throughput in items per minute (`ratePerInstancePerMin`).
The online lane targets a **tight** SLA (`inferSlaMinutes`, default ~30 min) so a burst is
drained fast; the batch lane targets a **relaxed** SLA (`batchSlaHours`, default ~24 h) so a
large job is spread cheaply over the window. Each denominator is floored at 1 (never divide
by zero) and each demand at 0 (never negative). `minInstances` sets a warm floor (always kept
alive); `maxScaleUpPerCycle` limits how many new units can be created per reconcile cycle to
prevent burst storms; `scaleDownStabilizationSeconds` holds the recent peak before shrinking.
This composes with scale-from-zero and the reap-only rule: the controller scales **up** to
whichever lane needs more and **never kills a busy worker** — workers drain and self-terminate.

## Hung-VM detection (ADR-012)

A running burst VM that stops sending heartbeats holds a GPU and billing budget while
delivering nothing. The gateway stamps a `received_at` field on every heartbeat write; the
autoscaler uses this server-side timestamp (not the agent's clock) to detect staleness and
escalate through three tiers:

| Tier | Trigger | Autoscaler action |
|---|---|---|
| **Unreachable** | `received_at` age > `vmHeartbeatStaleSeconds` | Exclude from capacity; provision a replacement |
| **Reset** | Still unreachable after `vmResetAfterSeconds` | Call `reset_instance` (soft reboot) |
| **Delete** | Still unreachable after `vmDeleteAfterSeconds` | Call `delete_instance`; remove from tracking ledger |

A **wedged** VM (crash-looping, e.g. OOM) is excluded after `wedgedRestartMax` warm-restart
attempts and flagged in the ledger for manual inspection. All four thresholds are tunable per
app in `scaling:` — see `docs/app-spec.md` for the field reference.

## External dependencies

| Dependency | Purpose | Driver / client |
|---|---|---|
| Queue | request IDs + batch file refs, two lanes per app (Redis Streams consumer groups; SQS) | `redis.asyncio`, `aiobotocore` |
| Object store | request bodies, results, spec store, cache | `aioboto3` (S3/MinIO), `gcloud-aio-storage` (GCS) |
| Cache | stockout board (Redis or object store — shared + persistent, see **ADR-005**) | pluggable |
| Kubernetes | create/observe worker pods, leader-election lease | `kubernetes_asyncio` |
| Terraform | burst VM provisioning (GCE, EC2) | rendered modules under `infra/terraform` |

## Decision records

See [`docs/adr/`](adr/README.md) for the architectural decisions and their rationale.
Brainstorming and planning notes are intentionally untracked (`.gitignore`); only
finalized ADRs and documentation live in the repo.
