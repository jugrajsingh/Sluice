# Runbook: cross-region VM burst

## How it works

1. Each App's spec expands into an **ordered candidate list**: `(pricing × substrate × location)`,
   spot first, Kubernetes before VMs within each pricing tier.
2. A pod stuck `Pending/Unschedulable` beyond `scheduleGraceSeconds` marks its candidate
   **stocked-out** in the shared cache (TTL `placement.stockout_ttl_s`, default 600 s) and the
   controller retries on the next candidate. The mark is **shared across all apps** — one probe
   spares everyone.
3. When every Kubernetes candidate is marked (or `placement.mode: vm`), the controller
   **Terraform-provisions a VM** in the first unmarked region (`plan -out` → `apply`; one
   Terraform state per VM, stored in the spec-store bucket under `sluice/apps/{app}/tf/...`).
   Provider errors are classified — `ZONE_RESOURCE_POOL_EXHAUSTED` / `InsufficientInstanceCapacity`
   mark the region and the next one is probed.
4. The VM's startup script launches the **agent** (`sluice_worker.vm_agent`, a container with the
   docker socket), which runs `workersPerVm` worker containers, heartbeats to
   `sluice/apps/{app}/vms/{id}/heartbeat.json`, and polls `desired.json` for commands.
5. **Scale-to-zero is emergent**: workers exit on empty queue → the agent lingers
   `lingerSeconds` (warm restarts happen here if the queue refills — the controller writes
   `desired.json: start_workers`) → the agent exits → the host script powers the VM off →
   the controller `terraform destroy`s the stopped instance.
6. **Spot preemption** is detected by the read-only instance-state prober (TERMINATED/stopped
   spot instance ⇒ `preempted`) → destroy + soft stockout mark + reschedule from the top of the
   candidate list, so work falls back to the cheapest available capacity automatically.

## Requirements

- **Globally reachable queue + object storage** (SQS / S3 / GCS class). An in-cluster Redis
  cannot serve a worker in another region — use `mode: kubernetes` if your backends are private.
- **GCE**: the controller credentials need `compute.instances.{insert,get,list,delete}` and
  `iam.serviceAccounts.actAs` for the worker service account; worker VMs get queue/bucket access
  via their **attached service account** (no key material on VMs).
- **EC2**: `ec2:RunInstances`, `ec2:DescribeInstances`, `ec2:TerminateInstances`,
  `ec2:CreateTags`, and `iam:PassRole` for the worker **instance profile**.
- The autoscaler image bundles the `terraform` binary and `infra/terraform/modules/`.

## Configuration

```yaml
# helm values
autoscaler:
  cloud:
    gcpCredentialsSecret: sluice-gcp     # or awsCredentialsSecret
  placement:
    prober: gce                          # gce | ec2
    stateBackend: { type: gcs, bucket: my-sluice-state }
```

Platform-level knobs (env on the autoscaler, `PLACEMENT__*`): `STOCKOUT_TTL_S`, `BOOT_DEADLINE_S`,
`TF_MODULE_DIR`, `TF_WORK_ROOT`, `PROVIDER_DEFAULTS` (JSON: `project`, `zone_suffix`,
`iam_instance_profile`).

## Inspecting state

- `sluice status <app>` / `GET /v1/apps/<app>` — phase, active candidate, worker + VM counts.
- `apps/{app}/status.json` in the bucket — the controller's full observed state.
- Stockout marks: cache keys `stockout/{substrate}/{provider}/{location}/{gpuType}/{pricing}`
  (e.g. `redis-cli KEYS 'stockout/*'` when the cache backend is redis).
- VM channel: `apps/{app}/vms/{id}/heartbeat.json` (agent → controller) and `desired.json`
  (controller → agent).
- Metrics: `sluice_stockouts_total`, `sluice_vms`, `sluice_holds_total`, `sluice_workers`.
