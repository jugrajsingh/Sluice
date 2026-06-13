# ADR-006: Multi-cluster, ordered, node-aware placement

**Date**: 2026-06-13
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Supersedes**: the candidate ordering and `candidate_key` shape of ADR-004 (its pure-`plan()`
and shared `StockoutBoard` mechanics are retained).
**Related**: ADR-001 (gateway broker), ADR-002 (short-lived worker credentials), ADR-005 (shared backends).

## Context

ADR-004 placed workers cheapest-first via an **implicit, pricing-dominant** candidate expansion
(`for pricing in p.pricing: …`), against a **single** Kubernetes cluster (the autoscaler's own).
Three gaps emerged:

- **Order isn't author-controlled.** "Spot across two clusters, then VM spot, then on-demand"
  can't be expressed; the code's pricing-first loop fixes the order.
- **One cluster only.** There's no way to run a (possibly GPU-less) Sluice in one cluster and
  place GPU workers in *other* clusters or clouds.
- **Node targeting is coarse.** GPU workloads must land on specific tainted node pools
  (`nvidia.com/gpu`), prefer a named pool first, and tolerate the taint — none of which the
  object-of-arrays schema expressed, and stuck pods were all treated as capacity stockouts even
  when the real cause was a config bug (an untolerated taint).

## Options Considered

### A: Keep the implicit pricing-dominant order (ADR-004)

**Pros**: no change. **Cons**: order not author-controlled; single cluster; no node targeting.
Rejected.

### B: Express fallback purely as weighted node affinity, let the scheduler decide

`preferredDuringSchedulingIgnoredDuringExecution` with descending weights gives intra-cluster
soft fallback in a single pod.

**Pros**: native, no controller logic. **Cons**: can't express cross-cluster or VM fallback, and
can't share stockout knowledge across apps. Insufficient on its own (kept as a possible future
intra-cluster refinement).

### C: Ordered candidate array + controller-driven walk + per-cluster pool

`AppSpec.placement` becomes an **ordered, discriminated list** (`kubernetes` | `vm`); the list
order *is* the priority. The controller walks it, routing each candidate to the right cluster
(or cloud) and classifying why a pod is unschedulable.

**Pros**: author controls order; uniform across clusters/VMs; reuses ADR-004's pure plan +
shared board. **Cons**: more controller logic than B (justified — B can't span clusters/VMs).

## Decision

**Approach C.**

- **Schema**: `placement: list[KubernetesCandidate | VmCandidate]`, discriminated on `type`; each
  entry is `{type, provider, spec}`. **Pricing lives inside `spec`** (k8s: encoded by spot
  node-pool labels; vm: a real provider parameter). A `KubernetesCandidate.spec` carries an
  **ordered `nodeSelectors` list** (targeted pool first, broader next), `tolerations`, and a
  per-candidate `scheduleGraceSeconds`. (Replaces `PlacementSpec`/`NodePoolSpec`; day-0, no
  back-compat.)
- **`candidate_key`** = `type/cluster/location/selector-hash/gpu/pricing` — so distinct selectors
  in one cluster, and the same selector in different clusters, stock out independently while
  sharing across apps.
- **Multi-cluster**: a k8s candidate's `provider` is `in-cluster` or a registered external cluster
  name. The controller holds a **pool of (pod manager, inspector) per cluster** — `in-cluster` via
  the in-cluster SA, external clusters via mounted kubeconfig Secrets listed in
  `AUTOSCALER__CLUSTERS`. One (possibly GPU-less) Sluice orchestrates many clusters and clouds.
- **Node-aware synthesis**: worker pods get the candidate's `nodeSelector`, the GPU resource
  limit, and synthesized `tolerations` (a default `nvidia.com/gpu:NoSchedule` toleration is added
  for GPU apps that don't declare one). The builder stays cloud-neutral — no auto-derived
  cloud-specific labels.
- **Unschedulable classification** (from the durable `PodScheduled=Unschedulable` message,
  preferred over droppable Events): **capacity** → stock out after the candidate's grace;
  **terminal capacity** (`max node group size reached`) → stock out immediately; **untolerated
  taint** → a config bug, surfaced on the app status and skipped this cycle only, **never** a
  persisted stockout and never churning the pod.

## Consequences

- Per-app order controls spot-vs-on-demand and cluster-vs-VM precedence; VM burst is just another
  candidate, not a hard-coded tier after Kubernetes.
- A GPU stockout in one cluster/selector no longer suppresses others; config bugs are surfaced
  rather than silently retried as stockouts.
- ADR-004's pure `plan()`, shared `StockoutBoard`, and `sluice.jugraj.dev/candidate` annotation
  are retained unchanged; only the expansion order and key shape change.
- Event-based scale-up handling (extend grace on `TriggeredScaleUp`) is deferred — the
  per-candidate grace window already covers cluster-autoscaler provisioning; we don't collect
  Events yet.
- Cross-cluster pod *networking* is the operator's responsibility: remote workers must reach the
  gateway broker URL and signed object-store URLs over HTTPS (ADR-001/002 mean no queue/bucket
  creds travel).
