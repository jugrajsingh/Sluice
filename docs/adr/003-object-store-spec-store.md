# ADR-003: Object-store spec store as the source of truth

**Date**: 2026-06-07
**Status**: Accepted
**Decision makers**: Jugraj + Claude

## Context

An app is defined by an `AppSpec` (image, handler, queue ref, storage prefix, resources,
scaling, placement). The platform needs a source of truth for these specs that keeps the
control plane stateless and lets the *same* app run on any substrate — in-cluster pods
today, burst VMs on other clouds tomorrow. Anything that pins app definitions to a single
Kubernetes cluster would block that goal and leave the control plane non-portable.

## Options Considered

### A: Store definitions inside the cluster (Kubernetes API objects)

**Pros**: native `kubectl` UX; watch semantics for free.
**Cons**: couples the source of truth to one cluster; needs cluster-admin to install;
the control plane can't be stateless or portable; no clean story for workers running on
VMs or other clouds. Rejected.

### B: External database (e.g. Postgres)

**Pros**: queryable; transactional.
**Cons**: another stateful dependency to run and back up; overkill for a small set of app
specs; the object store is already a hard dependency. Deferred (interface kept open).

### C: Object-store-backed spec store (kops-style)

App specs posted via the CLI / Admin API and stored in the object store
(`{bucket}/sluice/apps/{name}/spec.yaml` + `status.json`), behind an `AppRegistry`
interface over `ObjectStore`. Drivers: `objectstore` and `memory`.

**Pros**: control plane stateless and restartable; apps portable across substrates; no
cluster-admin install; the object store is already required; shared by gateway / console /
autoscaler. **Cons**: no native `kubectl` UX (replaced by CLI + Admin API).

## Decision

**Approach C.** `AppSpec` is the contract and the object store is the source of truth via
`AppRegistry`. The Kubernetes API is used only to create/observe bare worker pods and for
the leader-election lease — never as the app registry.

## Consequences

- The control plane is stateless and restartable; nothing app-defining lives in the cluster.
- Apps are portable: the same spec drives pods or VMs on any substrate.
- Apps are managed through the `sluice` CLI and the Admin API rather than `kubectl`.
- A Postgres registry remains a future option behind the same `AppRegistry` interface.
