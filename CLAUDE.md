# Sluice — Project Context

**Purpose:** Queue-driven, scale-from-zero GPU inference platform for Kubernetes.

Sluice lets the GPU control its own intake: a traffic burst **deepens a queue instead of
failing**, workers **self-terminate** (never killed mid-job), and GPU packing falls out of the
Kubernetes scheduler. You **bring only your model** — the SDK, charts, autoscaler, and gateway
do the rest. See `README.md` for the full pitch and serving modes.

## Architecture

Sluice is a `uv` workspace (`pyproject.toml` → `[tool.uv.workspace] members = ["packages/*"]`).
Interfaces (`Queue`, `ObjectStore`, `AppRegistry`, `Cache`, `InferenceObjects`, `ComputeProvider`)
are `Protocol`s with swappable drivers selected by config — swap Redis↔SQS or S3↔GCS with a
values change, no rebuild.

| Package | Role |
|---|---|
| `sluice-core` | interfaces, models, config + the driver **conformance suites** |
| `sluice-drivers` | `redis`/`sqs` queues, `s3`/`gcs`/`minio` stores, registry/cache drivers + config-driven factory |
| `sluice-autoscaler` | placement controller: reconcile, priced candidate escalation, stockout sharing, reap-only, leader election |
| `sluice-worker` | the poll→infer→exit worker loop + GPU lifecycle + VM agent + BYO-model archetypes |
| `sluice-gateway` | the stateless admission API (infer/status/batch, dedupe/cache, sync-sugar) |
| `sluice-console` | Admin API + live per-App dashboard with apply/delete/pause/resume/drain |
| `sluice-cli` | the `sluice` command: `apply` / `get` / `status` / `delete` / `pause` / `resume` |

Every production driver is validated by **subclassing the same conformance suite** the reference
(`memory`/`local`) drivers pass. Apps are plain YAML specs stored in a spec store (your
S3/GCS/MinIO bucket) — no CRDs, no Kubernetes objects in your hands.

## Development commands

```bash
uv sync                                 # set up the workspace venv from uv.lock
uv run pytest packages                  # run all workspace tests (importlib mode collects every package)
uv run ruff check .                     # lint (E, F, I, UP, B, ASYNC)
uv run ruff format .                    # format (120-col, py313)
uv run mypy .                           # type-check (strict)
```

Targets Python 3.13. Helm install + a BYO-model demo are covered in `README.md` (Quickstart) and
`examples/segmentation/`.

## Conventions & contributor notes

- Implement `load()`/`predict()`/`health()` against `sluice_worker.handler.BaseHandler` for your
  own model; declare `worker.instances` to pack N replicas on one GPU.
- The `samserve` example is **BYO and gitignored** (`examples/samserve/`) — it wraps a third-party
  model server and is never bundled. Use `examples/segmentation/` as the reference demo.
- The `/internal/v1` route prefixes are **architectural** (internal vs. public admission paths),
  not company-internal. Keep them.
- New driver? Subclass the matching conformance suite in `sluice-core` so it is validated the same
  way the reference drivers are.
- Licensed under AGPL-3.0 — see LICENSE.
