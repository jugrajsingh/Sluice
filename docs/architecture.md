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

Worker coordination and credentials are defined in **ADR-001** (gateway broker; queue
never exposed) and **ADR-002** (short-lived app-scoped JWT + pre-signed URLs). Workers
hold only a 6h app-scoped token and never reach the queue or master credentials.

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

## Placement

The autoscaler runs a pure placement playbook: cheapest viable candidate first
(spot zones → on-demand → cross-region VMs), with stuck-pod detection marking a
zone/GPU/pricing candidate as stocked out in a shared cache (shared across apps), then
walking to the next candidate. VM burst is provisioned via Terraform when Kubernetes is
exhausted and the app permits it. See **ADR-004** (priced placement playbook).

## External dependencies

| Dependency | Purpose | Driver / client |
|---|---|---|
| Queue | request IDs (Redis Streams consumer groups; SQS) | `redis.asyncio`, `aiobotocore` |
| Object store | request bodies, results, spec store, cache | `aioboto3` (S3/MinIO), `gcloud-aio-storage` (GCS) |
| Cache | stockout board, sync-result cache (Redis / object store / memory) | pluggable |
| Kubernetes | create/observe worker pods, leader-election lease | `kubernetes_asyncio` |
| Terraform | burst VM provisioning (GCE, EC2) | rendered modules under `infra/terraform` |

## Decision records

See [`docs/adr/`](adr/README.md) for the architectural decisions and their rationale.
Brainstorming and planning notes are intentionally untracked (`.gitignore`); only
finalized ADRs and documentation live in the repo.
