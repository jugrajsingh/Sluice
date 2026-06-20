# ADR-009: Bulk-batch inference (JSONL job API + object layout)

**Date**: 2026-06-18
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (gateway broker), ADR-003 (object-store spec store), ADR-010 (portable batch credentials)

## Context

The online admission path (`POST /v1/{app}/infer`) is tuned for low latency: one request, one
result, dedupe/cache in front. It is the wrong shape for large offline workloads — *"segment these
N images"*, where N is thousands to millions and the caller wants throughput and a resumable job,
not per-call latency. Cloud inference platforms expose this as a separate **bulk lane** (the
Vertex / Bedrock "batch prediction job" model): submit a manifest of input files, poll a job, then
collect output files. We want the same — one app serving both lanes — without standing up a second
service or a second model image.

## Options Considered

- **Fan out online `infer` calls client-side** — the caller drives concurrency and retries. Pushes
  all batching, checkpointing, and backpressure onto every client; no server-side resume; floods the
  online queue and the dedupe/cache. Rejected.
- **One queue message per record** — fine-grained, but millions of tiny messages overwhelm the
  queue and lose all locality (every record re-pays per-message overhead). Rejected.
- **One queue message per input file, JSONL records within (chosen)** — the file is the unit of
  work and the checkpoint boundary. A worker leases one file, streams its JSONL records, and
  checkpoints progress within the file. Coarse enough to keep the queue small, fine enough to
  parallelize across files and resume mid-file.

## Decision

A **3-stage job API** on the gateway, plus a fixed object layout, on a dedicated `{app}-batch` queue
(distinct from the online `{app}` queue so the two lanes never contend):

1. `POST /v1/{app}/batch` → `{job_id}` — opens a job, writes `manifest.json`.
2. `POST …/{job_id}/upload-url {filename}` → a presigned **PUT**; the client uploads each JSONL
   input file **directly** to the store (the gateway never proxies input bytes).
3. `POST …/{job_id}/submit {files:[…]}` — enqueues **1 message per file** on `{app}-batch`.

Progress and results are read back with:

- `GET …/{job_id}` — aggregates per-file status into job progress.
- `GET …/{job_id}/output` — returns presigned **GET** URLs for the gzipped output parts.

**Object layout** (under the data plane store):

```
apps/{app}/batch/{job_id}/
  input/{file}
  output/{file}.part-{offset:09d}.jsonl.gz
  status/{file}.json
  manifest.json
```

Each output record echoes the input record's `_rid` (fallback `"{filename}:{lineno}"`), so callers
can join outputs back to inputs regardless of ordering or partitioning.

**Resume-from-checkpoint ordering.** For each partition the worker writes the **output part first**,
then the **status checkpoint** (`records_done`). On redelivery (lease expiry, crash, requeue) the
worker resumes from `records_done`. A crash *between* the two writes reprocesses exactly one
partition — and because the output key is offset-named (`part-{offset:09d}`), reprocessing
**idempotently overwrites** that one key. Nothing is lost; nothing is duplicated.

## Consequences

- **One app, two lanes.** The same model image and handler serve online `infer` and bulk batch; the
  lane is selected by queue, not by a separate deployment.
- **Per-file granularity.** Parallelism, retries, and checkpoints are all at file granularity;
  within a file, resume is at `records_done` granularity.
- **Direct client upload / direct presigned download.** Input and output bytes never transit the
  gateway — only the small control calls (open / upload-url / submit / status / output) do. (How
  the worker writes those bytes without holding store credentials is ADR-010.)
- **Known limit — batch bypasses the online `_rid` cache.** Batch records are inferred on the
  `{app}-batch` lane and do **not** consult the gateway's online dedupe/cache. A record already
  computed online will be re-inferred in a batch job (and vice versa). Accepted: the two lanes have
  different freshness and cost models, and unifying the cache across them is out of scope here.
