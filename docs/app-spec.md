# App spec (`app.yaml`) reference

An app is one YAML document, applied with `sluice apply -f app.yaml`, stored in the object store
(the spec store is the source of truth — ADR-003). This documents every field, **where in the
control plane it is consumed**, and how it behaves on Kubernetes vs burst VMs.

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
  storage: { prefix: apps/seg }  # object-store prefix; defaults to apps/<name>
  resources: { gpu: 1, gpuType: nvidia-l4, cpu: 4, memoryGb: 20 }
  worker:                        # archetype + packing — see "Worker archetypes & packing"
    type: handler                # handler | sidecar
    instances: 3                 # replicas packed onto the one GPU the unit owns
  scaling: { messagesPerWorker: 24, maxWorkers: 0, scaleUpCount: 3, cooldownSeconds: 30, scheduleGraceSeconds: 180 }
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
scaling: { messagesPerWorker: 24, maxWorkers: 0, scaleUpCount: 3, cooldownSeconds: 30, scheduleGraceSeconds: 180 }
```

| Field | Default | Meaning |
|---|---|---|
| `messagesPerWorker` | 10 | Desired **units** = `ceil(queue_visible / messagesPerWorker)`. Tune it to a **packed unit's** throughput (≈ per-replica × `instances`). |
| `maxWorkers` | 0 (unbounded) | Hard cap on total units across pods + VMs. |
| `scaleUpCount` | 3 | Max pods created per reconcile cycle (ramp limiter). |
| `cooldownSeconds` | 30 | Quiet window after a scale-up. |
| `scheduleGraceSeconds` | 180 | App-level Pending grace; overridden per k8s candidate by `spec.scheduleGraceSeconds`. |

A pod and a VM each count as **one unit** (a unit packs `instances` replicas internally). A still-
loading sidecar unit (startup probe not yet passed) counts as *starting* — live, never stocked out.

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
