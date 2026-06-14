# ADR-008: Capability-scoped credential secrets (no Workload Identity)

**Date**: 2026-06-15
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (gateway broker), ADR-002 (short-lived worker credentials), ADR-005 (shared backends), ADR-006 (multi-cluster placement)

## Context

The control plane (gateway, console, autoscaler) needs credentials for several distinct external
systems — object store, queue, cache, VM-burst compute — and a deployment may **mix clouds** (S3
storage + Redis queue + GCE VMs, or GCS + SQS + EC2, …). Two prior shapes were wrong:

- **Workload Identity / IRSA** (binding a cloud IAM identity to a Kubernetes ServiceAccount via an
  `iam.gke.io/gcp-service-account` annotation) was explicitly rejected: *"we will not have workload
  identity for now, it will be a secret mounted."* Credentials are provisioned **externally** by the
  operator and mounted — the application never acquires them from the platform.
- A **single catch-all secret** (`backendSecret`, `AWS_*` envFrom on every component) conflated
  storage + queue creds, and a separate autoscaler-only `awsCredentialsSecret` collided with it
  (both injected `AWS_*`). The GCS key file was mounted only on the autoscaler, so gateway (which
  signs V4 URLs) and console (which reads the spec store) could not reach a GCS bucket at all.

## Decision

Credentials are **capability-scoped, externally-provisioned, mounted secrets** — never Workload
Identity. Each capability declares an optional secret, mounted **only on the components that use it**:

| Capability | Components | Channel |
|---|---|---|
| `credentials.storage` | gateway, console, autoscaler | S3/MinIO → `AWS_*` env; GCS → mounted JSON file (`GOOGLE_APPLICATION_CREDENTIALS`) |
| `credentials.queue` | gateway, console, autoscaler | SQS → `AWS_*`; Redis → `QUEUE__OPTIONS` (url w/ password) |
| `credentials.cache` | autoscaler | Redis → `CACHE__OPTIONS`; default object-store cache **inherits `storage`** |
| `credentials.compute` | autoscaler | EC2 → `AWS_*`; GCE → mounted JSON file. Inherits `storage` ambiently if same account |
| `broker.signingKeySecret` | gateway, autoscaler | shared HS256 `signing-key` (ADR-002) |
| `clusters[].kubeconfigSecret` | autoscaler | one kubeconfig file per external cluster (ADR-006) |

Each capability secret supports **`envFromSecret`** (keys → env) and/or **`file`** (mounted at
`/var/secrets/cred-<cap>/<key>`, with an env var — default `GOOGLE_APPLICATION_CREDENTIALS` — set to
that path). The app-registry and object-store cache hold no own creds; they inherit `storage`.

Operators may point several capabilities at the **same** Secret (one all-access identity) or at
distinct Secrets to separate clouds. Image pull is a separate `imagePullSecrets` value (empty for the
public ghcr images; set when mirrored privately).

## Consequences

- Any cloud combination is expressible by setting the relevant per-capability secrets; storage creds
  reach all three control-plane components (the GCS gateway/console gap is closed).
- The only ServiceAccounts are plain Kubernetes RBAC (autoscaler: pods + leases; console: read
  pods/events). No cloud-IAM-bound SA, no `iam.gke.io`/`serviceAccountAnnotations` anywhere.
- Within one process, AWS env and `GOOGLE_APPLICATION_CREDENTIALS` are ambient/global, so two
  capabilities using the same channel with **different** accounts would collide (last wins). For now
  they share one identity (all access), which is harmless; the per-capability split is what later
  enables file/profile-based separation (the drivers already accept explicit keys — the factory just
  passes `None` today).
- **Re-introducing Workload Identity, IRSA, or any cloud-IAM-bound ServiceAccount contradicts this
  ADR.** It is a standing constraint.
