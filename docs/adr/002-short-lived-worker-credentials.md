# ADR-002: Short-lived, app-scoped worker credentials (JWT + pre-signed URLs)

**Date**: 2026-06-13
**Status**: Accepted
**Decision makers**: Jugraj + Claude
**Related**: ADR-001 (the gateway broker that issues these grants)

## Context

Given the gateway broker (ADR-001), a worker still needs to read a request body, write a
result, and prove it may lease/ack a particular app's queue — all while running on
ephemeral, possibly-untrusted infrastructure. The requirement: **a worker must never hold
a master credential**; it may hold only short-lived, narrowly-scoped grants that expire on
their own. The control plane (gateway, console, autoscaler) holds the master credentials,
mounted from externally-created secrets (the operator creates a secret containing the GCS
JSON key, mounted as `GOOGLE_APPLICATION_CREDENTIALS`, or S3 / S3-compatible access+secret
env; no Workload Identity for now).

Two sub-problems: how a worker authenticates to the broker, and how it reads/writes objects.

## Options Considered

### Object access

- **Mount the master key on every worker** — propagates the master credential to untrusted
  hosts. Rejected (defeats the requirement).
- **Per-provider scoped temp credentials (e.g. AWS STS)** — provider-specific; no neutral
  equivalent for GCS / OVH. Rejected as the primary mechanism.
- **Pre-signed URLs (chosen)** — the gateway, holding the master key, signs a GET for the
  request body and a PUT for the result, each scoped to one object, one verb, short expiry.
  Provider-neutral (S3, GCS, MinIO all support it); this is GitLab's model.

### Worker authentication

- **Long-lived shared token** — broad, standing credential on untrusted hosts. Rejected.
- **Sliding renewal / refresh token** — a token that renews itself by being presented is a
  long-lived renewable bearer credential: stolen once, kept alive forever. Making it safe
  needs rotation + reuse-detection + a revocation store — heavy for a worker that lives
  minutes. Rejected (see "Why no refresh token").
- **Fixed-TTL JWT minted by the issuer (chosen)** — the autoscaler creates every worker, so
  it mints a hard-expiry, app-scoped JWT at creation and injects it; the gateway verifies.
  No self-renewal.

## Decision

**Pre-signed GET/PUT URLs for object I/O, and a fixed-TTL app-scoped JWT for broker auth.**

- JWT, HS256, signed with a server-only key shared between autoscaler (sign) and gateway
  (verify). Claims: `{app, worker_id, iss: "sluice-autoscaler", aud: "sluice-gateway",
  iat, exp}`. **TTL = 6h, configurable.** No sliding renewal, no refresh endpoint.
- `ObjectStore.signed_url(key, *, method, expires_s)` signs both GET and PUT. Real
  implementations for S3/MinIO (botocore) and GCS (V4 signing with the mounted key);
  `local`/`memory` stores (dev/tests) fall back to gateway-proxied URLs.
- A worker that outlives its token gets `401` and exits gracefully (a pod is respawned with
  a fresh token if work remains). For long-lived VM-burst workers, the **autoscaler**
  re-mints and pushes a fresh token through the agent `desired.json` channel —
  control-plane-driven renewal, never worker-driven. (Follow-up; 6h covers the common case.)

### Why no refresh token

Refresh tokens exist for clients whose issuer cannot reach them, so they self-renew. Here
the issuer (autoscaler) *creates* the worker and has a push channel, so issuer-push renewal
is strictly safer and avoids putting a renewable long-lived credential on the worker.
**Revisit trigger**: only if workers are ever decoupled from the issuer.

## Consequences

- A worker holds exactly one credential: a 6h app-scoped JWT. It reaches only the gateway
  broker and per-object pre-signed URLs — never Redis, broad bucket access, master creds,
  or another app's data.
- Master credentials stay confined to the control plane, mounted from external secrets.
- HTTPS is mandatory worker↔gateway (especially cross-cloud VMs).
- The autoscaler gains a JWT-minting step and a signing-key secret; the gateway gains JWT
  verification. The worker SDK gains an HTTP object-I/O path via signed URLs.

## Pending Security Review — replayable worker JWT

**Status: OPEN — accepted risk for this iteration; revisit before high-trust / multi-tenant
production.**

**Concern.** The worker JWT is a bearer token: anyone who obtains it can replay it until
`exp`. For cross-cloud VM workers the token traverses public networks, widening the
interception surface versus in-cluster pods.

**Mitigations in place.** (1) HTTPS in transit; (2) hard 6h `exp`, no sliding/refresh — a
stolen token dies at `exp` and cannot be kept alive; (3) tight scope — one app, the four
broker verbs, and per-object/per-verb/short-expiry signed URLs; (4) server-only signing key.

**Residual risk.** A token intercepted within its valid window can be replayed to
lease/ack/nack one app's messages and read/write that app's request/result objects, for up
to the remaining TTL.

**Deferred hardening (master↔worker), to evaluate before high-trust production.**

- mTLS client certificates worker↔gateway → sender-constrained, non-replayable transport.
- Sender-constrained tokens (DPoP / token binding) → a stolen bearer token is useless
  without the holder's key.
- `jti` + revocation list → kill a known-leaked token before `exp`.
- Shorter TTL + issuer-push renewal → smaller replay window without worker churn.
- Per-worker source-IP / pod-identity binding at the gateway.

**Revisit trigger.** Multi-tenant deployment, untrusted worker infrastructure, or any
compliance requirement for non-replayable worker credentials.
