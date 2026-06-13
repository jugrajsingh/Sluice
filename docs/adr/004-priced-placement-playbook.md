# ADR-004: Priced placement playbook with shared stockouts

**Date**: 2026-06-07
**Status**: Accepted
**Decision makers**: Jugraj + Claude

## Context

GPU capacity is scarce and priced: spot is cheaper but preemptible and frequently stocked
out in a given zone; on-demand is dearer but available. Workers must be placed
cheapest-first, fall back across zones and pricing tiers, and — when Kubernetes is
exhausted and the app permits — burst to VMs in other regions or clouds. A stockout
discovered by one app (e.g. "spot L4 in this zone is unavailable") should help every other
app rather than each rediscovering it through failed scheduling.

## Options Considered

### A: Static node selector only (let Kubernetes decide)

**Pros**: trivial.
**Cons**: no pricing fallback, no cross-zone walk, no VM burst, no shared stockout
knowledge; a spot stockout just wedges pods at Pending. Rejected.

### B: Imperative controller with live price feeds

The controller queries live prices and tries candidates imperatively.

**Pros**: cost-optimal in principle.
**Cons**: live price feeds are a heavy, provider-specific dependency; imperative branching
is hard to test and reason about. Deferred (cost-aware ordering is future work).

### C: Pure `plan()` over a pricing-ordered candidate list + shared stockout board

A pure function maps current state → actions; candidate expansion is pricing-dominant
(spot k8s-zones → spot VM-regions → on-demand …); a `StockoutBoard` over the `Cache`
(TTL'd, shared across apps) records unavailable candidates.

**Pros**: deterministic and table-testable; provider-neutral; stockouts shared across apps
for free via the cache. **Cons**: static pricing order, not live-price-optimal (acceptable).

## Decision

**Approach C.** A pure `plan()` emits actions (`CreatePods`, `ReapPod`, `RemoveStuckPod`,
`MarkStockout`, `ProvisionVms`, …). `candidate_key` =
`substrate/provider/location/gpu/pricing`. A stuck pod (Pending/Unschedulable past the
grace period) → `RemoveStuckPod` + `MarkStockout`, then the playbook walks to the next
candidate. VM burst is provisioned via Terraform when Kubernetes is exhausted and the app
declares VM (or both) placement.

## Consequences

- Cheapest-viable placement with automatic zone/pricing fallback and cross-region/cloud
  burst.
- Stockouts are shared across apps through the cache, avoiding repeated failed scheduling.
- The placement core is a pure function — covered by table tests.
- Each worker pod is stamped with a candidate annotation (`sluice.jugraj.dev/candidate`,
  see ADR for the namespace) so a stuck pod maps back to the candidate to mark stocked out.
- Live cost-aware ordering (Option B) is deferred behind the same candidate model.
