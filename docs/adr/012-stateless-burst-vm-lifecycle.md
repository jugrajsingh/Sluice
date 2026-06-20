# ADR-012: Stateless burst-VM lifecycle

**Date**: 2026-06-19
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-006 (placement), ADR-008 (capability-scoped credentials), ADR-010 (portable batch & VM creds), ADR-011 (data/control store split)

## Context

Burst VMs (GCE/EC2 spot) were created by terraform with **one persistent remote state object per VM**
and torn down by `terraform destroy` run in a per-VM working directory. Two problems followed:

- **State accumulation.** A remote backend (gcs/s3) never deletes state objects — only empties them on
  destroy — so per-VM state files piled up (≈30 from test bursts). And `destroy` needs the *local*
  working directory, which is lost on an autoscaler pod restart → orphaned VMs that nothing can reap.
- **Disk leak.** Spot preemption and the agent's idle self-termination both used **STOP**
  (`instance_termination_action = "STOP"`; a guest `shutdown -h now`). A STOPPED instance keeps — and
  bills for — its boot disk indefinitely.

A separate question: a VM can be RUNNING per the cloud yet do no useful work (agent/kernel hang, or a
worker that OOM-crash-loops). It then counts as capacity, so no replacement is provisioned and the
queue stalls — while a GPU bills for nothing.

## Options Considered

- **Keep per-VM persistent state** — status quo; the accumulation + orphan-on-restart problems above.
- **Per-(app,region) `for_each` shared state** — fewer objects, but a **mass-destroy failure mode**: a
  prober blip yields an incomplete `alive` set → a shrunk `for_each` → `apply` destroys healthy VMs.
  **Rejected** — sharing teardown fate across a region is too dangerous.
- **Stateless: ephemeral local state + cloud-as-truth (chosen)** — terraform only *creates*; the cloud
  (prober) is the source of truth; deletes are direct cloud-API calls. No shared state, no mass-destroy.

## Decision

**Create.** `terraform apply` runs in an **ephemeral LOCAL-state** working directory (no `backend`
block) to create one immutable VM, then the directory is **discarded**. (`keep_workdir` retains it for
debugging.) No remote state, no per-VM/region state object.

**Source of truth = the cloud, reconciled with a Sluice-owned ledger.** The **prober** (cloud API) is
the real-time liveness oracle; a per-(app,region) **VM-tracking ledger** (`{state}/sluice/apps/{app}/vms/{region}/tracking.json`,
ADR-011) is durable bookkeeping — the VMs Sluice provisioned, their status, and provision/preemption/prober
**errors**. The ledger is **not** terraform state: a plain JSON list + capped event log, no diff/plan/destroy/lock.
Reconcile sizes the fleet from the **prober's** running set, never the ledger (the ledger only adds
blip-resilience + an error log).

**Delete.** Spot preemption uses `instance_termination_action = "DELETE"` (EC2: spot `terminate` +
`instance_initiated_shutdown_behavior = terminate`); the agent **self-deletes** on idle
(`gcloud compute instances delete $SELF`, fallback `shutdown -h now`); and the autoscaler reaps via a
direct **API delete-by-name** (`delete_instance`). Boot disks are `auto_delete = true` (explicit), so a
delete frees the disk. The reconcile loop is the **backstop/janitor** for the residue self-delete can't
reach — confirmed-STOPPED leftovers and dead-by-heartbeat RUNNING VMs — not the everyday teardown path.

**Discovery** requires BOTH the app label and the **managed label** (`sluice-managed=true`, set by the TF
module): GCE `labels.sluice-app={app} AND labels.sluice-managed=true`, EC2 the equivalent tag filter — so
Sluice can never count or reap a VM it did not create.

**Hung-VM detection.** The gateway stamps a trusted **`received_at`** on each `/internal/v1/vm/heartbeat`
(the VM has no trusted clock). The autoscaler derives, per RUNNING VM:
- **`unreachable`** — no heartbeat for `vmHeartbeatStaleSeconds` (default 180s) → excluded from capacity
  (a replacement is provisioned), then **reset** at `vmResetAfterSeconds` (600s), then **deleted** at
  `vmDeleteAfterSeconds` (1200s) if the reboot didn't recover it;
- **`wedged`** — heartbeating but workers keep exiting past `wedgedRestartMax` (default 3) → excluded +
  flagged for the operator (rebooting/replacing a misconfigured app is futile).

## Consequences

- **No state cruft, no orphans.** Nothing persistent to accumulate or lose on restart; the old
  `…-tfstate` bucket is **decommissioned** (ADR-011).
- **No disk leak.** DELETE everywhere + explicit `auto_delete` ⇒ a freed instance frees its disk.
- **Safety = two independent reap justifications, never on a prober blip.** (1) cloud-confirmed-STOPPED;
  (2) dead-by-heartbeat — RUNNING per the prober but heartbeat silent past the hard deadline *and* a reset
  failed. The latter is sound because the heartbeat shares the broker **work** channel (can't heartbeat ⟺
  can't work) and is independent of the prober. Reconcile **never deletes when the prober errors** (it logs
  to the ledger and backs off), so a blip can only *over-provision* (self-correcting), never destroy.
- **A hung VM never holds a GPU forever** — it is excluded within ~3 min (workload protected by a
  replacement) and reclaimed within ~20 min.
- **Per-cloud parity.** GCE and EC2 modules mirror the create/terminate/disk semantics; the autoscaler
  reaps by name+zone via the prober for either cloud.
