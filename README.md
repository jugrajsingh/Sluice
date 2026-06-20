# Sluice

**A queue-driven, scale-from-zero GPU inference platform for any Kubernetes.**

You **bring only your model** — Sluice gives it a queue to sit behind, GPUs that scale
from zero, and two ways to feed it: live requests (**online**) and giant JSONL files
(**batch**). A traffic burst **deepens the queue instead of failing**, and workers
**self-terminate** when it drains (never killed mid-job). It is the open-source,
vendor-neutral implementation of the managed "SageMaker-Async / Vertex-Batch" pattern:
the GPU controls its own intake, and GPU packing falls out of the Kubernetes scheduler.
Like Knative, the SDK, charts, autoscaler, and gateway do the rest.

## Why

- **Burst-proof intake** — a traffic burst deepens a queue, never `503`s. Clients get a
  wait-time (ETA) instead of failure.
- **Never kill a busy worker** — workers own their lifecycle and self-exit; the control
  plane only scales **up** and **reaps exited** pods. It structurally cannot kill a
  running worker.
- **Scheduler-placed GPU pods** — workers carry a normal pod spec (`nvidia.com/gpu` + node
  selectors + GPU tolerations); the kube-scheduler places each on the matching pool. Which
  pool/cluster/VM to try, and in what order, is the autoscaler's placement playbook (ADR-006).
- **State-aware scaling** — on a GPU stockout (`ZONE_RESOURCE_POOL_EXHAUSTED`) the
  autoscaler **HOLDs** instead of piling up unschedulable pods, and surfaces the reason.
- **Ordered, multi-cluster placement** — each app lists `placement` candidates in priority
  order (in-cluster GPU pools, external clusters by kubeconfig, then Terraform-provisioned
  burst VMs). When a candidate is stocked out the autoscaler advances to the next, so a
  (possibly GPU-less) Sluice can place workers across many clusters and clouds. Workers only
  need the queue and the bucket, so it doesn't matter where they run. See
  [docs/runbook-vm-burst.md](docs/runbook-vm-burst.md) and `docs/adr/006`.
- **Pack the GPU, no MPS** — declare `worker.instances` to pack N model replicas on one GPU:
  `handler` images run N `BaseHandler` processes via a sequential launcher; `sidecar` images keep an
  unmodified HTTP model server fed by a Sluice queue-adapter. Per-candidate, so an L40S packs more
  than an L4. See `docs/app-spec.md` and ADR-007.
- **Interface-first & config-driven** — Queue, ObjectStore, AppRegistry, Cache,
  InferenceObjects are `Protocol`s with swappable drivers selected by config. Swap
  Redis↔SQS or S3↔GCS with a values change, no rebuild.

## Serving modes (one core)

Exactly three modes, all on `/v1/{app}/{lane}`. `/infer` is **async-first** (it enqueues
priority work) with sync-sugar on top — not a real-time promise.

1. **Online (async-first, with sync-sugar)** — `POST /v1/{app}/infer`. The body is your
   model's request JSON, with an optional top-level `"_rid"`. Flow: derive the key from
   `_rid` (or `sha256(body)`) → cache check (instant `200` if a result already exists) →
   else write the request, enqueue to `{app}-infer`, and short long-poll → `200` with the
   result if a warm worker finishes within `T_sync`, otherwise
   `202 {ticket, retry_after}` + a `Retry-After` header (ETA from queue depth).
2. **Async poll** — `GET /v1/{app}/status/{ticket}` → `200` result or `202` + `Retry-After`.
3. **Batch (24h-SLA lane)** — a presigned-upload workflow for large JSONL files:
   - `POST /v1/{app}/batch` → `{job_id}`.
   - per file: `POST /v1/{app}/batch/{job_id}/upload-url {filename}` → `{url}` (a presigned
     PUT — the client uploads its JSONL **directly** to the bucket).
   - `POST /v1/{app}/batch/{job_id}/submit {files:[…]}` → enqueues **one message per file**
     on `{app}-batch` (1 batch message = 1 file).
   - poll `GET /v1/{app}/batch/{job_id}` for progress.
   - `GET /v1/{app}/batch/{job_id}/output` → presigned GET URLs for the gzipped output parts;
     the client downloads and gunzips them.

   Batch files resume **from checkpoint** if a spot VM dies mid-file.

### Idempotency & correlation: `_rid`

`_rid` is the idempotency + cache key (think Stripe's `Idempotency-Key`). Set it on an
**online** request body and a repeat is a free `200` that never wakes a GPU; omit it and the
gateway hashes the body instead. In **batch**, every output record echoes its `_rid`
(falling back to `"{filename}:{lineno}"`) so outputs join cleanly back to inputs.

> **Known limitation:** batch records bypass the gateway cache, so a `_rid` already cached
> from an online call is still re-inferred when it appears in a batch file.

## Quickstart

```bash
helm install sluice charts/sluice                       # gateway + autoscaler + console
sluice apply -f examples/segmentation/app.yaml          # describe your app; Sluice places it
curl -X POST http://<gateway>/v1/topwear/infer --data-binary @image.jpg
```

Apps are plain YAML specs stored in a **spec store** (your S3/GCS/MinIO bucket, kops-style)
via the `sluice` CLI or the Admin API — no CRDs, no Kubernetes objects in your hands.
See [`examples/segmentation`](examples/segmentation/) for the full BYO-model demo, and
implement `load()`/`predict()`/`health()` against `sluice_worker.handler.BaseHandler` for
your own model.

For a production Helm deployment (required values, credential wiring, ingress/TLS, burst-VM
prerequisites) see **[docs/helm-deploy.md](docs/helm-deploy.md)**.

## CLI (`sluice`)

A thin, kubectl-style client for the Admin API (the **console**, not the gateway). Install only
the client — cloud drivers are an optional extra:

Not on PyPI yet — install from the tagged source (the script builds the `sluice` command locally):

```bash
curl -LsSf https://raw.githubusercontent.com/jugrajsingh/Sluice/main/install.sh | bash
#   SLUICE_VERSION=v0.2.2 …   pin a release      SLUICE_DIRECT=1 …   add the [direct] extra
```

Or build it yourself with uv (thin: typer + httpx + sluice-core, no cloud drivers):

```bash
uv tool install "git+https://github.com/jugrajsingh/Sluice.git@v0.2.2#subdirectory=packages/cli" \
  --with "git+https://github.com/jugrajsingh/Sluice.git@v0.2.2#subdirectory=packages/core"
# once published:  pip install sluice-cli   ·   pip install 'sluice-cli[direct]'
```

Point it at a secured console and authenticate with an API key — by flag, env, or a saved context
(precedence: `--api-key` > `SLUICE_API_KEY` > active context):

```bash
export SLUICE_API=https://sluice-console.example.net
export SLUICE_API_KEY=…                  # sent as X-API-Key on every call
# …or persist a named context:
sluice config set-context prod --api https://sluice-console.example.net --api-key …
sluice config use-context prod
```

```bash
sluice validate -f app.yaml     # strict-validate locally (typos error — see below)
sluice apply -f app.yaml        # validate + apply; --dry-run to diff, --direct to bootstrap
sluice apply -f app.yaml --dry-run   # show field-level diff against the stored spec; no write
sluice get                      # list apps (-o table|json|yaml|wide); shows phase/reason/candidate
sluice describe my-app          # full detail: spec, status, workers, active candidate
sluice logs my-app -f           # stream worker logs (k8s pods; VM workers not yet supported)
sluice pause | resume | drain my-app
sluice delete my-app            # prompt for confirmation; --yes / -y to skip
sluice schema                   # print the App-spec JSON schema
sluice version                  # print client version + ping the console for server version
```

**Config file** lives at `~/.config/sluice/config.yaml` (created automatically by
`sluice config set-context`). The `api_key_env` field lets you store an environment-variable
*name* instead of a literal key — useful for secret-manager-backed keys:

```yaml
# ~/.config/sluice/config.yaml (chmod 600)
current-context: prod
contexts:
  prod:
    api: https://sluice-console.example.net
    api_key_env: SLUICE_API_KEY_PROD   # sluice reads os.environ[api_key_env] at runtime
```

**Shell completion** — install once per shell:

```bash
sluice --install-completion       # bash / zsh / fish auto-detected
```

App YAML is **strictly validated**: an unknown or mistyped field is rejected with a
"did you mean …?" hint instead of being silently dropped.

## Packages

| Package | Role |
|---|---|
| `sluice-core` | interfaces (`Queue`, `ObjectStore`, `AppRegistry`, `Cache`, `InferenceObjects`, `ComputeProvider`), models, config + the driver **conformance suites** |
| `sluice-drivers` | `redis`/`sqs` queues, `s3`/`gcs`/`minio` stores, registry/cache drivers + a config-driven factory |
| `sluice-autoscaler` | the placement controller: reconcile, priced candidate escalation, stockout sharing, reap-only, leader election |
| `sluice-worker` | the poll→infer→exit worker loop + GPU lifecycle + VM agent + BYO-model archetypes |
| `sluice-gateway` | the stateless admission API (infer/status/batch, dedupe/cache, sync-sugar) |
| `sluice-console` | Admin API + live per-App dashboard with apply/delete/pause/resume/drain |
| `sluice-cli` | the `sluice` command: `apply`/`validate`/`get`/`describe`/`delete`/`pause`/`resume`/`drain`/`logs`/`schema`/`config` |

## Driver matrix

| Interface | Reference (built-in) | Production drivers |
|---|---|---|
| `Queue` | `memory` | `redis` (Streams), `sqs` |
| `ObjectStore` | `local` | `s3`, `minio` (S3 + endpoint), `gcs` |
| `AppRegistry` | `memory` | `objectstore` (any ObjectStore backend) |
| `Cache` | `memory` | `redis`, `objectstore` |

Every production driver is validated by **subclassing the same conformance suite** the
reference drivers pass.

## Queues, storage & credentials

**Per-app queues (symmetric).** Online work lands on `{app}-infer`; batch work lands on
`{app}-batch` (one message per file). Same shape, two lanes.

**Object layout.** Storage splits into a **data** store (inference + batch objects, rooted at
`AppData/{app}` by default) and a **control/state** store (`sluice/` prefix, durable). See
`docs/architecture.md` for the full layout table. Data paths:

```
AppData/{app}/requests/{rid}                               # online request (raw)
AppData/{app}/results/{rid}.gz                             # online result (gzipped)
AppData/{app}/batch/{job_id}/input/{file}                  # uploaded JSONL
AppData/{app}/batch/{job_id}/output/{file}.part-NNNNNNNNN.jsonl.gz   # output parts (gzipped)
AppData/{app}/batch/{job_id}/status/{file}.json            # per-file progress
AppData/{app}/batch/{job_id}/manifest.json                 # job manifest
```

Results and batch output parts are **always gzip-compressed** (hence the `.gz` keys). The
gateway serves an online result with `Content-Encoding: gzip` so the HTTP client inflates it
transparently — or gunzips it first for a client that doesn't accept gzip. Batch output is
pulled via a presigned GET URL and gunzipped by the client.

**Credential model** (see `docs/adr/001`, `002`, `008`). A worker — Kubernetes pod or burst
VM — holds only a short-lived, **app-scoped JWT** and reaches everything through the gateway
broker (queue leases + presigned object URLs); it **never** holds backend storage or queue
credentials. So the same app can run cross-cloud (e.g. a burst VM on one cloud against object
storage on another). Master credentials stay on the control plane (mounted secrets).

## What's new

- **Per-app `uploadTtlHours`** is now honored: the batch-cleanup CronJob uses the per-app
  `batch.uploadTtlHours` window to reap abandoned uploads, falling back to the chart-level
  `batchCleanup.ttlHours` default.
- **`sluice get` / `sluice describe`** now surface the persisted `phase`, `reason`, and active
  `candidate` written by the autoscaler — operators see the canonical placement state, not a
  recomputed approximation.
- **Autoscaler per-app status logs**: the autoscaler emits a structured log line on every
  phase change (Healthy / Scaling / Held counts, reason, active candidate), so operators no
  longer have to infer state from `/metrics` alone.

## License

This project is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0) — see [LICENSE](LICENSE).

Running a modified version over a network obligates you (under AGPL §13) to offer its complete
source to users. A separate **commercial license** that lifts these copyleft/source-disclosure
terms (e.g. to run a proprietary managed service) is available — see [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)
or contact **jugrajskhalsa@gmail.com**.

Contributors: see [CONTRIBUTING.md](CONTRIBUTING.md). A Contributor License Agreement (CLA) is
required before a pull request can be merged — the one-click signing flow is described there.
