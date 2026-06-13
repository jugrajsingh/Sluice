# Architecture Decision Records

This directory records architectural decisions made while designing and building
Sluice. Each ADR captures one decision with its context, the options considered,
and the rationale. Brainstorming and planning notes are kept out of the repo
(see `.gitignore`); only finalized decisions and documentation are tracked here.

**Scope**: queue-driven, scale-from-zero GPU inference — control plane, data plane,
worker coordination, and credentials.

## Index

Numbered in the order recorded; the **Date** field in each ADR is the decision date.

| ADR | Title | Date | Status |
|---|---|---|---|
| [001](001-gateway-as-worker-broker.md) | Gateway brokers worker coordination; the queue is never exposed | 2026-06-13 | Accepted |
| [002](002-short-lived-worker-credentials.md) | Short-lived, app-scoped worker credentials (JWT + pre-signed URLs) | 2026-06-13 | Accepted |
| [003](003-object-store-spec-store.md) | Object-store spec store as the source of truth | 2026-06-07 | Accepted |
| [004](004-priced-placement-playbook.md) | Priced placement playbook with shared stockouts | 2026-06-07 | Accepted (ordering superseded by ADR-006) |
| [005](005-shared-persistent-backends-only.md) | Deployable backends are shared and persistent only | 2026-06-13 | Accepted |
| [006](006-multi-cluster-ordered-placement.md) | Multi-cluster, ordered, node-aware placement | 2026-06-13 | Accepted |
| [007](007-worker-archetypes-and-packing.md) | Worker archetypes (handler / sidecar) and GPU packing | 2026-06-14 | Accepted |

## Template

```
# ADR-NNN: Title

**Date**: YYYY-MM-DD
**Status**: Proposed | Accepted | Superseded by ADR-XXX
**Decision makers**: ...

## Context
## Options Considered
## Decision
## Consequences
```
