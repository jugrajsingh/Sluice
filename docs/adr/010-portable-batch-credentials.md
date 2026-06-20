# ADR-010: Portable batch & VM-agent credentials (broker-only, no ambient store creds)

**Date**: 2026-06-18
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (gateway broker), ADR-002 (short-lived worker credentials), ADR-008 (capability-scoped credentials), ADR-009 (bulk-batch inference)

## Context

An earlier batch implementation had burst-VM workers — and the VM agent — write to the object store
using **ambient / attached cloud credentials** (the VM's instance role / attached service account).
That only works when the **compute cloud equals the storage cloud**: a GCE VM's attached service
account cannot sign S3 requests, and an EC2 instance role cannot sign GCS requests. It also violated
ADR-002's standing requirement — *"a worker holds only a short-lived JWT"* — by giving the worker a
broad, ambient store credential. For Sluice's whole premise (burst into whatever GPU capacity is
cheapest, regardless of where the store lives), credential portability across clouds is mandatory.

## Options Considered

- **Attached cloud identity on the VM (status quo)** — same-cloud only; broad ambient credential on
  untrusted compute. Rejected (the bug this ADR fixes).
- **Mount the master store key on burst VMs** — portable, but propagates the master credential to
  untrusted hosts; directly contradicts ADR-002. Rejected.
- **Route all worker / VM-agent object I/O through the gateway broker (chosen)** — the worker holds
  only its JWT (ADR-002); the gateway, which already holds the master store credential per ADR-008,
  mints presigned URLs or proxies small control writes. Provider-neutral and ADR-002-compliant.

## Decision

**All worker and VM-agent object I/O goes through the gateway broker.** The channel is chosen by
object size and role:

- **Large objects — batch output parts → broker-minted on-demand presigned PUT.** The worker asks
  the broker for a PUT URL and streams the gzipped part directly to the store. Bytes never transit
  the gateway.
- **Small control JSON — batch per-file status, VM heartbeat + command → broker-proxied.** The
  gateway (holding the master key) performs the tiny store read/write on the worker's behalf. These
  are small enough that proxying costs nothing and avoids minting a URL per heartbeat.
- **Batch input → presigned GET at lease time.** When the broker hands a worker a `{app}-batch`
  lease, it includes a presigned GET for that file's input object.

Output parts are **always gzip-compressed** (the `.gz` key from ADR-009). The autoscaler **stops
injecting `OBJECT_STORE__*`** onto VM workers entirely — a burst VM is handed its JWT and nothing
else store-related.

## Consequences

- **Cross-cloud portability.** A worker (in-cluster pod or burst VM) holds only its JWT and works
  against any store — e.g. a GCE burst VM writing to an S3 *or* GCS bucket. Compute cloud and storage
  cloud are fully decoupled.
- **RAM-bounded output.** The output partition is spilled to a temp file and gzipped off-thread, then
  streamed to the presigned PUT once — the whole partition is never held in memory.
- **The gateway holds the master store credential.** This is by design (control-plane only, per
  ADR-008): only the gateway signs data URLs and proxies VM state. Workers stay credential-minimal.
- **One more broker responsibility.** The gateway gains presigned-PUT minting for output parts and
  proxied read/write for control JSON, on top of the GET/PUT it already signs for online I/O
  (ADR-002). The blast radius if the gateway is compromised is unchanged — it already held the master
  key.
