# Helm deployment guide

Sluice ships as a single Helm chart (`charts/sluice`) that installs the gateway, autoscaler,
and console as Kubernetes Deployments, plus optional bundled Redis and MinIO. Workers are bare
pods synthesized from app specs — they are never part of the chart.

## Prerequisites

- Kubernetes 1.27+ with NVIDIA device plugin (for GPU workers)
- Helm 3.12+
- An external queue backend (Redis or SQS) and object store (S3, GCS, or MinIO) — or enable
  the bundled Redis/MinIO for development
- Secrets pre-created in the namespace (see below)

## Minimal install (development / GPU-less)

The bundled Redis and an external MinIO-compatible store are enough to run a control plane
without a GPU node. Workers can still run on any node that has access to the queue and bucket.

```bash
helm install sluice charts/sluice \
  --set apiKeySecret=sluice-api-key \
  --set broker.signingKeySecret=sluice-signing-key \
  --set redis.enabled=true \
  --set object_store.backend=minio \
  --set minio.enabled=true \
  --set minio.credentialsSecret=sluice-minio-creds
```

## Required values

### `apiKeySecret`

Name of a Kubernetes `Secret` with a key `api-key`. This key gates the gateway's public
admission API (`/v1/*`) and the console's app-management API. If left empty, those APIs are
**open** — set it before exposing the gateway publicly.

```bash
kubectl create secret generic sluice-api-key --from-literal=api-key=<random-hex>
```

### `broker.signingKeySecret`

Name of a `Secret` with a key `signing-key` (an HS256 shared secret). The autoscaler mints
worker JWTs with this key; the gateway verifies them. Required for any worker to authenticate.

```bash
kubectl create secret generic sluice-signing-key \
  --from-literal=signing-key=$(openssl rand -hex 32)
```

### `broker.url`

The URL workers use to reach the gateway broker. **This must be externally reachable** whenever
any external cluster or burst VM is used — remote workers cannot resolve in-cluster Service DNS.

```yaml
broker:
  url: https://sluice-gw.example.com   # ingress/LB hostname; must resolve from outside the cluster
```

For a cluster-only deploy (no burst VMs, no external clusters) the default
`http://sluice-gateway` (in-cluster Service) is fine.

### `object_store` and `state_store`

Configure the data store and (optionally) a separate control/state store. See ADR-011.

```yaml
object_store:
  backend: gcs                     # s3 | gcs | minio
  options: { bucket: my-sluice-data }

# Optional: split state into its own bucket for independent lifecycle TTLs
state_store:
  backend: gcs
  options: { bucket: my-sluice-state }
```

When `state_store.backend` is empty it inherits `object_store` — a single-bucket deploy.

### `queue`

```yaml
queue:
  backend: redis                   # redis | sqs
  options: {}                      # passed as QUEUE__OPTIONS JSON; e.g. { url: redis://...:6379 }
```

For managed Redis (e.g. Memorystore, ElastiCache) set `redis.enabled: false` and supply the
connection URL via `credentials.queue.envFromSecret` or directly in `queue.options`.

## Capability-scoped credentials (ADR-008)

Each credential capability is mounted **only on the components that use it**. This lets you
mix clouds (e.g. SQS queue + GCS store + GCE burst VMs) and gives each component the minimum
access it needs.

```yaml
credentials:
  # Object DATA store — gateway (presign), console (spec store), autoscaler (spec + VM state)
  storage:
    envFromSecret: sluice-s3-creds     # Secret keys: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    # OR for GCS:
    file:
      secret: sluice-gcp-storage
      key: credentials.json
      env: GOOGLE_APPLICATION_CREDENTIALS

  # Control/STATE store — if using a separate state bucket with different creds
  state:
    file:
      secret: sluice-gcp-state
      key: credentials.json
      env: GOOGLE_APPLICATION_CREDENTIALS
    # Leave empty to inherit `storage` credentials (same account, one bucket)

  # Queue credentials — gateway, console, autoscaler
  queue:
    envFromSecret: sluice-sqs-creds    # SQS: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    # OR for Redis with auth: key QUEUE__OPTIONS={"url":"redis://:pw@host:6379/0"}

  # Cache — autoscaler only (defaults to object-store cache when empty)
  cache:
    envFromSecret: sluice-redis-cache

  # VM burst (GCE/EC2) — autoscaler only
  compute:
    file:
      secret: sluice-gcp-compute
      key: credentials.json
      env: GOOGLE_APPLICATION_CREDENTIALS
    # EC2: envFromSecret with AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
```

Point multiple capabilities at the **same Secret** when one identity has all the access
needed (last-wins is harmless — identical creds are mounted more than once).

## Gateway ingress / TLS

The gateway exposes two paths:
- `/v1/*` — public admission API (clients submit inference requests and batch jobs)
- `/internal/v1/*` — worker broker (lease/ack/heartbeat endpoints)

Both live on the same port. For burst VMs or external clusters to reach the broker, enable the
ingress and set `broker.url` to the external hostname:

```yaml
gateway:
  ingress:
    enabled: true
    className: nginx
    host: sluice-gw.example.com
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-prod
    tls:
      enabled: true
      secretName: sluice-gateway-tls   # cert-manager writes here; auto-named if left empty

broker:
  url: https://sluice-gw.example.com   # same host — workers and clients use this
```

If you serve clients and workers on the same hostname, a single ingress rule is enough.
Split them (separate hostnames/paths) only if you need to apply different network policies to
the broker endpoints.

## Batch cleanup CronJob

The batch-cleanup CronJob sweeps abandoned uploads (jobs that were never submitted or that
stalled). Enable it when you use the batch lane:

```yaml
batchCleanup:
  enabled: true
  schedule: "0 * * * *"   # hourly (default)
  ttlHours: 24             # delete pending_upload/running jobs older than this
```

The per-app `batch.uploadTtlHours` field in the app spec overrides `ttlHours` for that app.
Enable the CronJob if any of your apps declare a `batch:` block.

## Bundled dev backends

```yaml
redis:
  enabled: true              # bundled Redis StatefulSet; disable for managed Redis
  storage: 1Gi

minio:
  enabled: false             # bundled MinIO (S3-compatible); for dev/local testing
  storage: 10Gi
  credentialsSecret: sluice-minio-creds   # Secret: root-user + root-password (required when enabled)
```

Set both to `false` in production and supply external backend details via `credentials.*`.

## Multi-cluster external workers

To orchestrate workers in remote clusters, add each cluster's kubeconfig as a Secret and list
it under `clusters`:

```yaml
clusters:
  - name: gke-east
    kubeconfigSecret: sluice-kubeconfig-gke-east
    namespace: default
```

Apps target a cluster by name in `placement[].provider`. The implicit `in-cluster` provider is
always available without configuration.

## VM burst prerequisites

See [docs/runbook-vm-burst.md](runbook-vm-burst.md) for the full burst-VM runbook. The Helm
knobs:

```yaml
autoscaler:
  placement:
    prober: gce                  # gce | ec2
    workerBaseImage: ghcr.io/jugrajsingh/sluice-worker-base:0.1.0
    providerDefaults:
      project: my-gcp-project
      zone_suffix: a
      service_account_email: sluice-vm@my-gcp-project.iam.gserviceaccount.com
```

## Full production example

```yaml
image:
  tag: "0.1.0"
  pullPolicy: IfNotPresent

apiKeySecret: sluice-api-key
broker:
  signingKeySecret: sluice-signing-key
  url: https://sluice-gw.example.com

queue:
  backend: redis
  options: {}

object_store:
  backend: gcs
  options: { bucket: my-sluice-data }

state_store:
  backend: gcs
  options: { bucket: my-sluice-state }

credentials:
  storage:
    file: { secret: sluice-gcp, key: credentials.json, env: GOOGLE_APPLICATION_CREDENTIALS }
  state:
    file: { secret: sluice-gcp, key: credentials.json, env: GOOGLE_APPLICATION_CREDENTIALS }
  queue:
    envFromSecret: sluice-redis   # key QUEUE__OPTIONS={"url":"redis://:pw@memorystore:6379/0"}
  compute:
    file: { secret: sluice-gcp, key: credentials.json, env: GOOGLE_APPLICATION_CREDENTIALS }

redis:
  enabled: false   # using managed Memorystore
minio:
  enabled: false

gateway:
  replicas: 2
  ingress:
    enabled: true
    className: nginx
    host: sluice-gw.example.com
    tls: { enabled: true }

batchCleanup:
  enabled: true
  schedule: "0 * * * *"
  ttlHours: 24

autoscaler:
  placement:
    prober: gce
    workerBaseImage: ghcr.io/jugrajsingh/sluice-worker-base:0.1.0
    providerDefaults:
      project: my-gcp-project
      zone_suffix: a
      service_account_email: sluice-vm@my-gcp-project.iam.gserviceaccount.com
  leaderElection: true
```

## Verifying the install

```bash
helm lint charts/sluice
helm template sluice charts/sluice -f my-values.yaml | kubectl apply --dry-run=client -f -
helm install sluice charts/sluice -f my-values.yaml
kubectl rollout status deploy/sluice-gateway deploy/sluice-autoscaler deploy/sluice-console
sluice version   # pings the console to confirm connectivity
```
