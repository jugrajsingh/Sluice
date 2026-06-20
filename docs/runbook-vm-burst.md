# Runbook: cross-region VM burst

## How it works (ADR-012 — stateless ephemeral lifecycle)

1. Each App's `placement` is an **author-ordered candidate list** (`kubernetes` | `vm`); list
   order is the priority. A k8s candidate expands to one attempt per node selector; a vm candidate
   to one per region. See ADR-006.
2. A pod stuck `Pending/Unschedulable` is classified from its `PodScheduled` message: capacity
   exhaustion marks its candidate **stocked-out** in the shared cache (TTL `placement.stockout_ttl_s`,
   default 600 s; immediately when the node group is maxed, otherwise after the candidate's
   `startupGraceSeconds`) and the controller retries the next candidate. An untolerated-taint
   **config bug** is surfaced, not stocked out. The mark is **shared across all apps** — one probe
   spares everyone.
3. When every Kubernetes candidate is stocked out (or the app lists only `vm` candidates), the
   controller **Terraform-provisions a VM** in the first unmarked region. Terraform runs with an
   **ephemeral local state** (no remote `stateBackend`, no GCS/S3 state bucket — ADR-012). The
   state exists only for the duration of the `terraform apply`; after that the cloud API itself
   is the source of truth, probed at every reconcile cycle.
   Provider errors are classified — `ZONE_RESOURCE_POOL_EXHAUSTED` / `InsufficientInstanceCapacity`
   mark the region stocked out and the next one is probed.
4. The VM's startup script launches the **agent** (`sluice_worker.vm_agent`, a container with the
   docker socket), which runs the unit for the app's `worker` archetype — a launcher packing
   `worker.instances` `worker.run` processes (handler), or the model server + adapter (sidecar) —
   heartbeats to the broker at `sluice/apps/{app}/vms/{id}/heartbeat.json`, and polls
   `sluice/apps/{app}/vms/{id}/desired.json` for commands. The gateway stamps `received_at` on
   each heartbeat; the autoscaler uses this timestamp for hung-VM detection (see below).
5. **Scale-to-zero is emergent**: workers exit on empty queue → the agent lingers
   `lingerSeconds` (warm restarts happen here if the queue refills — the controller writes
   `desired.json: start_workers`) → the agent exits → the host script powers the VM off.
6. **Stopped/self-terminated VMs** are detected by the read-only instance-state prober
   (TERMINATED/stopped instance) → the VM record is **deleted by name** via the autoscaler's
   `delete_instance` API call (no `terraform destroy`, no stored state needed). Spot instances
   are configured with `instance_termination_action = DELETE` (GCE) or
   `instance_initiated_shutdown_behavior = terminate` (EC2), so the cloud automatically reclaims
   the VM on power-off — the autoscaler's delete call is a belt-and-suspenders reap.
7. **Spot preemption** is detected by the prober (TERMINATED/stopped spot) → soft stockout mark +
   reschedule from the top of the candidate list, so work falls back to the cheapest available
   capacity automatically.

## VM-tracking ledger

The autoscaler maintains a per-(app, region) **tracking ledger** in the control/state store:

```
sluice/apps/{app}/vms/{region}/tracking.json
```

This JSON file is the live inventory of all VMs the autoscaler provisioned for the app in that
region. It records VM IDs, states, provisioning timestamps, and last-heartbeat times. Because
Terraform state is ephemeral (discarded after each apply), the ledger is the only durable record
of what VMs exist. The prober cross-checks the ledger against the cloud API every reconcile cycle
and reconciles discrepancies (e.g. a VM the cloud reports as gone is removed from the ledger).

To inspect the ledger for an app:
```bash
# GCS
gsutil cat gs://<state-bucket>/sluice/apps/<app>/vms/<region>/tracking.json | jq .
# S3/MinIO
aws s3 cp s3://<state-bucket>/sluice/apps/<app>/vms/<region>/tracking.json - | jq .
```

## Hung-VM detection and escalation (ADR-012)

A running VM that stops sending heartbeats is a **hung VM** — it holds a GPU and consumes
billing while delivering nothing. Sluice detects and remediates these automatically:

| State | Trigger | Action |
|---|---|---|
| **Unreachable** | `received_at` heartbeat age > `vmHeartbeatStaleSeconds` (default 180 s) | VM is excluded from capacity count; a replacement is provisioned immediately |
| **Reset** | VM still unreachable after `vmResetAfterSeconds` (default 600 s) | `reset_instance` called on the cloud API — soft reboot without reprovisioning |
| **Delete** | VM still unreachable after `vmDeleteAfterSeconds` (default 1200 s) | `delete_instance` called — VM is terminated and removed from the ledger |

A **wedged** VM is one that crash-loops (e.g. OOM): the agent restarts repeatedly. After
`wedgedRestartMax` (default 3) warm-restart attempts the VM is excluded from capacity and
flagged in the ledger for the operator. The VM is not automatically deleted to preserve crash
evidence; delete it manually after inspection.

All four thresholds are tunable per app in `scaling:` — see `docs/app-spec.md` for details.

## Requirements

- **Globally reachable queue + object storage** (SQS / S3 / GCS class). An in-cluster Redis
  cannot serve a worker in another region — list only `kubernetes` candidates if your backends
  are private.
- **Globally reachable broker URL** (`broker.url` in Helm values). Burst VMs resolve this from
  outside the cluster; it **must** be an externally-reachable HTTPS endpoint (gateway ingress or
  load-balancer), not an in-cluster Service DNS name.
- **GCE**: the controller credentials need `compute.instances.{insert,get,list,delete,reset}` and
  `iam.serviceAccounts.actAs` for the worker service account.
- **EC2**: `ec2:RunInstances`, `ec2:DescribeInstances`, `ec2:TerminateInstances`,
  `ec2:CreateTags`, and `iam:PassRole` for the worker **instance profile**.
- The autoscaler image bundles the `terraform` binary and `infra/terraform/modules/`.
- There is **no Terraform remote state bucket** — do not configure `stateBackend` (that field no
  longer exists). State is ephemeral-local; the cloud API + the tracking ledger are the truth.

## Configuration

```yaml
# helm values (charts/sluice/values.yaml)
autoscaler:
  placement:
    prober: gce           # gce | ec2 | "" (no VM provisioning)
    workerBaseImage: ghcr.io/jugrajsingh/sluice-worker-base:0.1.0
    providerDefaults:
      project: my-gcp-project
      zone_suffix: a
      service_account_email: sluice-vm@my-gcp-project.iam.gserviceaccount.com

broker:
  url: https://sluice-gw.example.com   # MUST be externally reachable — burst VMs resolve this

credentials:
  compute:
    file:
      secret: sluice-gcp          # Secret with key credentials.json (GCE service-account JSON)
      key: credentials.json
      env: GOOGLE_APPLICATION_CREDENTIALS
```

Platform-level autoscaler env knobs (`PLACEMENT__*`): `STOCKOUT_TTL_S`, `BOOT_DEADLINE_S`,
`TF_MODULE_DIR`, `TF_WORK_ROOT`, `PROVIDER_DEFAULTS` (JSON).

Per-app hung-VM thresholds (in `scaling:` of the app spec):
`vmHeartbeatStaleSeconds`, `vmResetAfterSeconds`, `vmDeleteAfterSeconds`, `wedgedRestartMax`.

## Inspecting state

- `sluice get <app>` / `sluice describe <app>` — phase, active candidate, worker + VM counts,
  placement reason. The phase and reason are the controller's persisted view, not recomputed.
- `GET /v1/apps/<app>` — same data via the console Admin API.
- `sluice/apps/{app}/status.json` in the **state** bucket — the controller's full observed state.
- VM tracking ledger: `sluice/apps/{app}/vms/{region}/tracking.json` — see above.
- VM heartbeat / command channel:
  - `sluice/apps/{app}/vms/{id}/heartbeat.json` (agent → controller; `received_at` from gateway)
  - `sluice/apps/{app}/vms/{id}/desired.json` (controller → agent)
- Stockout marks: cache keys `stockout/{type}/{cluster}/{location}/{selector-hash}/{gpuType}/{pricing}`
  (e.g. `redis-cli KEYS 'stockout/*'` when the cache backend is Redis).
- Metrics: `sluice_stockouts_total`, `sluice_vms`, `sluice_holds_total`, `sluice_workers`.

## Operational scenarios

### A VM is stuck RUNNING but not processing work

1. Check `sluice describe <app>` for `unreachable` VMs in the status.
2. Inspect the heartbeat age: `sluice/apps/{app}/vms/{id}/heartbeat.json`.
3. If the VM is genuinely hung, the autoscaler will reset it at `vmResetAfterSeconds` and delete
   it at `vmDeleteAfterSeconds`. To force-delete immediately:
   ```bash
   # The autoscaler's delete-by-name reap via the cloud CLI
   gcloud compute instances delete <instance-id> --zone=<zone>   # GCE
   aws ec2 terminate-instances --instance-ids <id>               # EC2
   ```
   Then remove the entry from the tracking ledger (the next reconcile will also clean it up once
   the prober observes the instance is gone).

### Finding leftover STOPPED VMs (pre-ADR-012 or spot VMs not yet reaped)

Spot VMs are configured with `instance_termination_action = DELETE` (GCE) or
`instance_initiated_shutdown_behavior = terminate` (EC2), so they self-delete on shutdown.
If you have leftover STOPPED VMs from older deployments:

```bash
# GCE — list sluice-managed stopped instances
gcloud compute instances list \
  --filter="labels.sluice-managed=true AND status=TERMINATED" \
  --format="table(name,zone,status,machineType)"

# Delete them
gcloud compute instances delete <name> --zone=<zone>
```

No `terraform destroy` is needed — and no `terraform` command should be run manually against
Sluice-managed VMs, because there is no stored state to work from.
