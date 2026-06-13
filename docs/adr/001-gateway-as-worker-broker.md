# ADR-001: Gateway brokers worker coordination; the queue is never exposed

**Date**: 2026-06-13
**Status**: Accepted
**Decision makers**: Jugraj + Claude

## Context

Sluice runs workers on arbitrary infrastructure — in-cluster pods today, burst VMs on
AWS / GCP / OVH next. Sluice's contract keeps request bodies in object storage and
puts only the request ID on the queue. The open question was how a worker obtains work.

If workers connect to the queue directly:

- A self-hosted queue (Redis) reachable by workers across clouds is a DDoS surface and
  leaks a broad, long-lived credential onto ephemeral, possibly-untrusted hosts.
- SQS-style scoped temporary credentials only help AWS users — not provider-neutral.
- The control plane loses any central point to shape, prioritize, or meter consumption.

GitLab solves the analogous problem (object storage for Workhorse and CI runners) by
having the credentialed service mint narrow, expiring grants for everyone else, rather
than handing out the master credential. We want the same principle for the queue.

## Options Considered

### A: Workers connect to the queue directly (status quo)

**Pros**: simplest; no new endpoints.
**Cons**: exposes the queue to every worker host; broad credential on untrusted infra;
provider-specific; no central control point. Rejected.

### B: A dedicated dispatch component for worker traffic

A separate service brokers worker lease/ack, keeping the gateway client-only.

**Pros**: cleanest separation of client vs worker traffic.
**Cons**: a fourth deployable and more moving parts for no capability the gateway can't
already provide. Deferred unless worker traffic demands isolation.

### C: Gateway hosts the broker; autoscaler issues worker tokens

The gateway — which already holds the master queue + object-store credentials, already
faces clients (infer / status / batch), and already scales horizontally — also serves a
worker-facing, authenticated broker API. The autoscaler, which creates every worker,
mints that worker's token at creation; the gateway only verifies it.

**Pros**: reuses the component that already has the credentials and HTTP surface; one
"highway" for both client and worker traffic; horizontal scale via the shared queue
consumer group; clean issuer/verifier split; natural home for future priority scheduling.
**Cons**: the gateway carries both client and worker traffic (acceptable; it scales).

### D: Controller (autoscaler) hosts the broker

**Pros**: single authority over workers.
**Cons**: the controller is a leader-elected singleton reconcile loop; serving
data-plane worker traffic from it couples scaling decisions to request serving and
bottlenecks on the leader. Rejected.

## Decision

**Approach C.** The gateway is the single communication highway and hosts the
worker-facing broker; the autoscaler is the token issuer; the controller stays pure
control plane.

The broker exposes an authenticated, app-scoped API (`/internal/v1/*`):

| Endpoint | Action |
|---|---|
| `POST /lease {max}` | `XREADGROUP` as consumer = worker_id; return request IDs + pre-signed GET(body) + PUT(result) URLs + lease IDs |
| `POST /extend {lease_ids}` | reset idle time so a long job is not reclaimed mid-inference |
| `POST /ack {lease_id}` | `XACK` (+ remove) on completion |
| `POST /nack {lease_id}` | release for immediate redelivery on clean failure |

The broker composes existing interfaces — `Queue.receive/ack/extend` plus
`ObjectStore.signed_url` — behind the HTTP layer; the worker SDK becomes a pure broker
client. Credentials are covered in ADR-002.

## Consequences

- Workers never reach the queue; Redis (or any self-hosted queue) is never exposed.
- At-least-once is preserved: a `lease` runs `XAUTOCLAIM` of entries idle past the
  visibility window, so a dead worker's messages are redelivered without double-acking.
- The visibility window is sized above the typical (sub-minute) job; the worker SDK owns
  a background heartbeat (`extend`) for the rare long job, so handler code is untouched.
- The control plane gains a single point to later add priority / weighted scheduling
  (deferred — not built in this iteration).
- Request/result bytes flow worker↔storage directly via pre-signed URLs, keeping them off
  the gateway (GitLab's rationale for the same pattern).
- The worker SDK drops its direct Queue/ObjectStore usage; the autoscaler stops injecting
  queue/store backend configuration into worker pods.
