# App spec (`app.yaml`) reference

An app is one YAML document, applied with `sluice apply -f app.yaml`, stored in the object store
(the spec store is the source of truth — ADR-003). This documents every field, **where in the
control plane it is consumed**, and how it behaves on Kubernetes vs burst VMs.

> **Strict validation.** Every field below is validated strictly — an unknown or mistyped key
> (e.g. `maxVMs` instead of `maxVms`, `batchSLAHours` instead of `batchSlaHours`) is **rejected**,
> not silently dropped. Check a spec locally with `sluice validate -f app.yaml`, and print the
> canonical schema with `sluice schema`.

```yaml
apiVersion: sluice/v1
kind: App
metadata:
  name: segmentation            # required; defaults queue.ref and storage.prefix
spec:
  image: ghcr.io/acme/seg:1.2.0  # worker image
  handler: handler:SegHandler    # "module:Class" — your BaseHandler subclass (handler archetype)
  env: { HF_HUB_OFFLINE: "1" }   # extra env injected into every worker (see "Worker env")
  desiredState: Ready            # Ready | Paused
  queue: { ref: segmentation }   # queue name; defaults to metadata.name
  storage: { prefix: AppData/segmentation }  # object-store prefix; defaults to AppData/<name>
  resources: { gpu: 1, gpuType: nvidia-l4, cpu: 4, memoryGb: 20 }
  worker:                        # archetype + packing — see "Worker archetypes & packing"
    type: handler                # handler | sidecar
    instances: 3                 # replicas packed onto the one GPU the unit owns
  scaling:                       # online + dual-lane scaling math — see "scaling"
    messagesPerInstance: 24
    minInstances: 0
    maxInstances: 0
    maxScaleUpPerCycle: 3
    scaleUpCooldownSeconds: 60
    startupGraceSeconds: 300
    scaleDownStabilizationSeconds: 120
    putConcurrency: 8            # shared in-flight model-call budget per unit
    inferSlaMinutes: 30          # online SLA the dual-lane math targets
    ratePerInstancePerMin: 1000  # assumed per-instance throughput for the formula
  batch:                         # optional — enables the bulk-batch lane (see "batch")
    batchSlaHours: 24
    outputPartitionSize: 1000
  placement:                     # ordered list of candidates — see "Placement"
    - type: kubernetes
      provider: in-cluster
      spec: { pricing: spot, nodeSelectors: [{ cloud.google.com/gke-spot: "true" }] }
```

## Top-level fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `image` | str | `""` | The worker image (handler image, or an HTTP model-server image for `sidecar`). |
| `handler` | str | `""` | `module:Class` of your `BaseHandler` (handler archetype). |
| `env` | map | `{}` | Merged into worker env verbatim on **both** substrates — the channel for `MODEL__*`/`WORKER__*`/`HF_*` tuning. |
| `desiredState` | `Ready`\|`Paused` | `Ready` | `Paused` → reaping continues, no new units created. |
| `queue.ref` | str | `metadata.name` | Logical queue name; the request-ID stream. |
| `storage.prefix` | str | `apps/<name>` | Object-store prefix for bodies/results/spec. |

## `resources` — **sizes Kubernetes pods only**

```yaml
resources: { gpu: 1, gpuType: nvidia-l4, cpu: 4, memoryGb: 20 }
```

| Field | Kubernetes | VM | Notes |
|---|---|---|---|
| `gpu` | `requests`+`limits["nvidia.com/gpu"]` on the unit; adds the default `nvidia.com/gpu:NoSchedule` toleration | **ignored** (agent does `docker run --gpus all`) | **Integer = whole GPUs.** `gpu: 1` ⇒ the unit owns one full GPU and packs `worker.instances` replicas on it (no MPS). |
| `cpu` | pod cpu requests+limits | **ignored** | VM size comes from `placement[].spec.machineType`. |
| `memoryGb` | pod memory requests+limits (`<n>Gi`) | **ignored** | — |
| `gpuType` | **stockout-key segment only** — **not** a node selector | stockout-key segment only | Pin the GPU model via a label in `nodeSelectors` (k8s) or `acceleratorType` (VM). |

The autoscaler does not bin-pack by `resources` — k8s scheduling is delegated to the kube-scheduler
(requests + `nodeSelectors` + tolerations); VM sizing is `machineType` + the declared `instances`.

## `worker` — archetype & packing

```yaml
worker:
  type: handler                 # handler | sidecar
  instances: 3                  # replicas packed onto the one GPU this unit owns
  args: []                      # extra CLI args (sidecar: the model server's entrypoint args)
  server:                       # REQUIRED when type=sidecar
    port: 8080
    requestPath: /v1/segment
    method: POST
    contentType: application/json
    healthPath: /healthz
    readyTimeoutS: 600          # cold-start budget; the unit is "starting", not failed, until then
    concurrency: 0              # adapter in-flight cap; 0 ⇒ = instances
```

| `type` | What runs in the unit | Packing |
|---|---|---|
| `handler` | One container running the launcher (`sluice_worker.launch --instances N`), which starts N `worker.run` processes **sequentially** (each loads its own `BaseHandler` model copy). | N independent leasing processes on one GPU; sequential start avoids the parallel-load OOM. |
| `sidecar` | Two containers: your HTTP model server (its own entrypoint, packs itself via its own env, e.g. `SERVER__WORKERS`) + the Sluice **adapter** that feeds it. | Server packs N internally; the adapter keeps ~`instances` requests in flight over `localhost`. |

The sidecar adapter is **model-agnostic, verbatim passthrough**: the queue body is POSTed to
`server.requestPath` as-is and the HTTP response is stored as the result. It holds the JWT; the
model server holds no credentials. See ADR-007.

## `scaling` — counted in **units** (1 pod = 1 VM = 1 unit)

```yaml
scaling:
  messagesPerInstance: 24        # queue depth one unit absorbs
  minInstances: 0                # warm floor — always keep ≥ this many live
  maxInstances: 0                # hard ceiling (0 = unbounded)
  maxScaleUpPerCycle: 3          # ramp limiter — cap new units per reconcile
  scaleUpCooldownSeconds: 60     # debounce after a scale-up
  startupGraceSeconds: 300       # pod-schedule + VM-boot deadline
  scaleDownStabilizationSeconds: 120  # hold the recent peak before shrinking (anti-flap)
  putConcurrency: 8              # shared in-flight model-call budget per unit
  inferSlaMinutes: 30            # online SLA the dual-lane math targets
  ratePerInstancePerMin: 1000    # assumed per-instance throughput for the formula
```

| Field | Default | Meaning |
|---|---|---|
| `messagesPerInstance` | 10 | Desired **units** = `ceil(queue_visible / messagesPerInstance)`. Tune to a **packed unit's** throughput (≈ per-replica × `instances`). |
| `minInstances` | 0 | Warm floor — the autoscaler always keeps at least this many units alive. 0 = full scale-to-zero. |
| `maxInstances` | 0 (unbounded) | Hard cap on total units across all pods + VMs. 0 = no cap. |
| `maxScaleUpPerCycle` | 3 | Max new units created per reconcile cycle (ramp limiter — prevents burst storms on deep queues). |
| `scaleUpCooldownSeconds` | 60 | Quiet window after a successful scale-up before another is allowed (debounce). |
| `startupGraceSeconds` | 300 | Pod-schedule + VM-boot deadline. A unit still pending after this is classified as hung/stalled and the candidate is marked stocked out. |
| `scaleDownStabilizationSeconds` | 120 | Hold the recent desired-peak for this long before actually scaling down (anti-flap). |
| `putConcurrency` | 8 | Shared in-flight model-call budget **per unit** — the semaphore the dual-source scheduler arbitrates between the infer and batch lanes (infer prioritized). Caps concurrent `predict` calls regardless of which lane fed them. |
| `inferSlaMinutes` | 30 | Online SLA window the dual-lane scaling math targets; the unit count is sized so the visible infer backlog clears within it. |
| `ratePerInstancePerMin` | 1000 | Assumed per-instance throughput (records/min) the scaling formula uses to convert backlog + SLA into a desired unit count. |

A pod and a VM each count as **one unit** (a unit packs `instances` replicas internally). A still-
loading sidecar unit (startup probe not yet passed) counts as *starting* — live, never stocked out.

`putConcurrency`, `inferSlaMinutes`, and `ratePerInstancePerMin` feed the **dual-lane** scaling math:
when a `batch:` block is present, each unit serves both the online infer lane and the bulk-batch lane
off one shared `putConcurrency` budget (see "`batch`"). With no `batch:` block they govern the online
lane alone.

### Hung-VM detection thresholds (ADR-012)

These knobs control the unreachable/wedged VM escalation ladder. They live in `scaling:` alongside
the other autoscaler tuning.

| Field | Default | Meaning |
|---|---|---|
| `vmHeartbeatStaleSeconds` | 180 | A RUNNING VM whose gateway-stamped `received_at` heartbeat is older than this is classified **unreachable**. It is excluded from capacity (a replacement is provisioned) but not yet acted on. |
| `vmResetAfterSeconds` | 600 | If the VM is still unreachable after this many seconds it is **reset** (soft reboot via the cloud API: `reset_instance`). Reset is attempted once; if the VM recovers it re-enters the RUNNING pool. |
| `vmDeleteAfterSeconds` | 1200 | If the VM is still unreachable after this many seconds (reset did not help, or is not available for the provider) it is **deleted** (`delete_instance`). Spot-DELETE terminates the billing. |
| `wedgedRestartMax` | 3 | Maximum warm-restart attempts for a crash-looping ("wedged") VM (e.g. OOM loop). After `wedgedRestartMax` restarts the VM is excluded and flagged for the operator instead of being restarted again. |

## `batch` — optional bulk-batch lane

```yaml
batch:
  batchSlaHours: 24            # SLA window for the batch lane
  outputPartitionSize: 1000    # records per flushed part (…part-NNNNNNNNN.jsonl.gz, gzipped)
  maxVms: 4                    # cap on burst VMs for the batch lane
  uploadTtlHours: 24           # abandoned-upload cleanup window
  starveGraceMin: 7            # minutes of no batch progress before a starvation floor is reserved
```

Adding a `batch:` block **enables the bulk-batch lane** for the app. Every field is optional and
carries the default shown; an empty `batch: {}` is enough to turn the lane on. Records are batched
through the gateway's batch-submit path, processed off a separate `{app}-batch` queue, and flushed
to the object store as gzipped JSON-Lines parts.

| Field | Default | Meaning |
|---|---|---|
| `batchSlaHours` | 24 | SLA window for the batch lane — the deadline the dual-lane scaling math targets for the batch backlog (the bulk-lane analogue of `scaling.inferSlaMinutes`). |
| `outputPartitionSize` | 1000 | Output records per flushed part. Each part is written under `storage.prefix` as `…part-NNNNNNNNN.jsonl.gz` (zero-padded sequence, gzipped JSON-Lines). |
| `maxVms` | 4 | Cap on burst VMs the batch lane may add (independent of the online lane's `scaling.maxWorkers` and per-candidate `placement[].spec.maxVms`). |
| `uploadTtlHours` | 24 | Abandoned-upload cleanup window — staged batch inputs not finalized within this window are reaped. |
| `starveGraceMin` | 7 | Minutes of no batch progress before the on-box scheduler reserves a **starvation floor** for the batch lane, so a saturated infer lane cannot indefinitely block batch from the shared `putConcurrency` budget. |

When a `batch:` block is present, the autoscaler sets **`WORKER__BATCH_ENABLED`** on the workers
automatically — **you do not set it**. The **dual-source scheduler** then runs both the `{app}-infer`
and `{app}-batch` lanes on the **same worker unit**, drawing from the one shared `scaling.putConcurrency`
budget with **infer prioritized**; the `starveGraceMin` floor keeps batch from being starved when infer
is hot.

## Placement

`placement` is an **ordered list** of candidates; list order is the priority. Discriminated on
`type` (`kubernetes` | `vm`). See ADR-006 and `docs/runbook-vm-burst.md`. Each candidate may carry
**`overrides`** for heterogeneous GPUs:

```yaml
placement:
  - type: kubernetes
    provider: in-cluster              # in-cluster | <registered external cluster name>
    spec:
      pricing: spot
      nodeSelectors:                  # ORDERED: try [0] first, fall back to [1], ...
        - { cloud.google.com/gke-nodepool: l4-spot, gpu: l4, lifecycle: spot }
        - { cloud.google.com/gke-spot: "true" }
      tolerations:                    # optional; default nvidia.com/gpu toleration auto-added for GPU apps
        - { key: nvidia.com/gpu, operator: Exists, effect: NoSchedule }
      scheduleGraceSeconds: 180
    overrides: { instances: 3 }       # L4 (24 GB) packs 3
  - type: vm
    provider: gce                     # gce | ec2
    spec:
      pricing: spot
      machineType: g2-standard-48
      acceleratorType: nvidia-l40s
      regions: [us-central1]
      lingerSeconds: 120              # warm-idle window before the VM powers off
      maxVms: 3                       # cap on concurrent VMs for this app
    overrides: { instances: 6 }       # heterogeneous: an L40S (48 GB) packs more
```

`overrides` (any of `image`, `env`, `args`, `instances`) merge over the app-level `worker`/`image`/
`env` (override wins; `env` is merged), so each candidate can pack to its GPU.

## Worker env (set via `env`)

Worker-runtime knobs; set through `spec.env`. The autoscaler injects the broker URL/token and, for
sidecar, the `WORKER__SERVER_*` config — you don't set those.

| Env | Default | Meaning |
|---|---|---|
| `WORKER__BATCH_SIZE` | 8 | Jobs a handler process leases per loop (`predict(batch)`). Intra-process batching lever. |
| `WORKER__MAX_JOBS` | 5000 | Jobs a worker processes before voluntarily exiting (recycle). |
| `WORKER__MAX_BLANK_RETRIES` | 3 | Empty leases in a row before exiting (drives scale-to-zero). |
| `WORKER__HEARTBEAT_S` | 50 | Lease-extend heartbeat interval. |
| `WORKER__BATCH_ENABLED` | `"0"` | Set to `"1"` to activate the dual-source scheduler (batch lane). **The autoscaler sets this automatically when a `batch:` block is present — you do not set it.** Listed here for completeness; overriding it manually is not supported. |
| `MODEL__*`, `SERVER__*`, `HF_HUB_OFFLINE`, … | — | Your model's env; reaches workers on **both** substrates. |

## Known gaps / sharp edges

1. **Packing is operator-declared, not bin-packed.** You set `instances` per candidate (you do the
   VRAM/CPU/RAM math offline, as a SamServe deployment sets `SERVER__WORKERS`). Sluice does **not**
   probe a VM and compute how many fit — no `vram` field, no dynamic bin-packing.
2. **One VM = one app.** A VM is never shared across apps (or across app instances of different
   apps); multi-tenant VM packing is out of scope.
3. **`gpuType` is informational** (stockout key), not a node selector — pin it via `nodeSelectors`
   (k8s) or `acceleratorType` (VM).
4. **Cluster-autoscaler scale-up events aren't consumed.** A capacity-stuck pod is stocked out after
   the per-candidate grace; the grace window (not a `TriggeredScaleUp` event) is what covers a
   legitimate node scale-up.
