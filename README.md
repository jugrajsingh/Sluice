# Sluice

**A queue-driven, scale-from-zero GPU inference platform for any Kubernetes.**

Sluice is the open-source, vendor-neutral implementation of the managed
"SageMaker-Async / Vertex-Batch" pattern: the GPU controls its own intake, bursts
**queue instead of failing**, workers **self-terminate** (never killed mid-job), and
GPU packing falls out of the Kubernetes scheduler. Like Knative, you **bring only your
model** — the SDK, charts, autoscaler, and gateway do the rest.

## Why

- **Burst-proof intake** — a traffic burst deepens a queue, never `503`s. Clients get a
  wait-time (ETA) instead of failure.
- **Never kill a busy worker** — workers own their lifecycle and self-exit; the control
  plane only scales **up** and **reaps exited** pods. It structurally cannot kill a
  running worker.
- **Scheduler-driven GPU packing** — workers carry a normal pod spec
  (`nvidia.com/gpu` + MPS node pool); the scheduler packs them, requesting a new GPU
  only when the current one is full. No custom placement logic.
- **State-aware scaling** — on a GPU stockout (`ZONE_RESOURCE_POOL_EXHAUSTED`) the
  autoscaler **HOLDs** instead of piling up unschedulable pods, and surfaces the reason.
- **Ordered, multi-cluster placement** — each app lists `placement` candidates in priority
  order (in-cluster GPU pools, external clusters by kubeconfig, then Terraform-provisioned
  burst VMs). When a candidate is stocked out the autoscaler advances to the next, so a
  (possibly GPU-less) Sluice can place workers across many clusters and clouds. Workers only
  need the queue and the bucket, so it doesn't matter where they run. See
  [docs/runbook-vm-burst.md](docs/runbook-vm-burst.md) and `docs/adr/006`.
- **Interface-first & config-driven** — Queue, ObjectStore, AppRegistry, Cache,
  InferenceObjects are `Protocol`s with swappable drivers selected by config. Swap
  Redis↔SQS or S3↔GCS with a values change, no rebuild.

## Serving modes (one core)

1. **Online (sync-sugar)** — `POST /v1/{model}/infer`: cache-hit → `200`; else enqueue +
   short long-poll → `200` if a warm worker finishes within `T_sync`, else
   `202 + ticket + Retry-After(ETA)`.
2. **Async** — poll `GET /v1/{model}/status/{ticket}` → `200 + result` or `202 + ETA`.
3. **Batch** — `POST /v1/{model}/batch` (JSONL/base64) → `batch_id`; poll
   `GET /v1/{model}/batch/{id}`.

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

## Packages

| Package | Role |
|---|---|
| `sluice-core` | interfaces (`Queue`, `ObjectStore`, `AppRegistry`, `Cache`, `InferenceObjects`, `ComputeProvider`), models, config + the driver **conformance suites** |
| `sluice-drivers` | `redis`/`sqs` queues, `s3`/`gcs`/`minio` stores, registry/cache drivers + a config-driven factory |
| `sluice-autoscaler` | the placement controller: reconcile, priced candidate escalation, stockout sharing, reap-only, leader election |
| `sluice-worker` | the poll→infer→exit worker loop + GPU lifecycle + VM agent + BYO-model archetypes |
| `sluice-gateway` | the stateless admission API (infer/status/batch, dedupe/cache, sync-sugar) |
| `sluice-console` | Admin API + live per-App dashboard with apply/delete/pause/resume/drain |
| `sluice-cli` | the `sluice` command: `apply` / `get` / `status` / `delete` / `pause` / `resume` |

## Driver matrix

| Interface | Reference (built-in) | Production drivers |
|---|---|---|
| `Queue` | `memory` | `redis` (Streams), `sqs` |
| `ObjectStore` | `local` | `s3`, `minio` (S3 + endpoint), `gcs` |
| `AppRegistry` | `memory` | `objectstore` (any ObjectStore backend) |
| `Cache` | `memory` | `redis`, `objectstore` |

Every production driver is validated by **subclassing the same conformance suite** the
reference drivers pass.

## License

Apache-2.0 — see [LICENSE](LICENSE).
