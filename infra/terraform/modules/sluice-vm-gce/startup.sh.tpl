#!/bin/bash
# Sluice burst-VM bootstrap. Written to be idempotent on a GPU-ready image (GCP Deep Learning VM,
# which ships docker + NVIDIA driver + nvidia-container-toolkit) — every install branch below
# no-ops there — while still installing docker on a bare Ubuntu/Debian image.
set -euo pipefail

# 1. docker (present on the Deep Learning VM image; installed here for a bare Ubuntu/Debian image).
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh

# 2. Authenticate docker to GCR / Artifact Registry using the VM's ATTACHED service account
#    (ambient ADC via the metadata server) so it can pull the private worker-base + model images.
#    Idempotent; skipped if gcloud is absent.
command -v gcloud >/dev/null 2>&1 && gcloud auth configure-docker gcr.io --quiet || true

# 3. GPU runtime. The Deep Learning VM (GPU) image already has the NVIDIA driver + container toolkit,
#    so `docker run --gpus all` works out of the box. On a bare Ubuntu/Debian image, install the
#    driver + nvidia-container-toolkit here before the agent starts a GPU container.
if ! nvidia-smi >/dev/null 2>&1; then
  echo "sluice: no NVIDIA driver detected — GPU containers will fail. Use a GPU-ready boot image" \
       "(e.g. a Deep Learning VM image), or add driver + nvidia-container-toolkit install here." >&2
fi

# 3.5 Pre-pull the model (app) image on the HOST, where docker is authenticated to GCR via the
#     attached SA (step 2). The agent launches the model server by shelling out to `docker run`
#     INSIDE its own container, whose docker config has NO GCR credential helper — an in-container
#     pull reaches GCR anonymously and 403s on a private image. Pulling here (authed) means the
#     agent's `docker run` finds the image already present locally and never needs to authenticate.
#     The worker-base image is pulled (authed) by the final `docker run` below on the host.
APP_IMAGE='${replace(lookup(env, "APP_IMAGE", ""), "'", "'\\''")}'
if [ -n "$APP_IMAGE" ]; then
  docker pull "$APP_IMAGE"
fi

# 4. Run the Sluice VM agent. It docker-runs the model server (app image) + the adapter/launcher
#    (worker-base image), supervises them, and returns when idle past the linger window — then we
#    power the VM off so a stuck/idle burst VM never bills indefinitely.
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add "$(stat -c '%g' /var/run/docker.sock)" \
  -e VM_ID="$(hostname)" \
  -e SLUICE_APP="${app}" \
  -e WORKER_IMAGE="${worker_image}" \
  -e WORKERS_PER_VM="${workers_per_vm}" \
  -e LINGER_SECONDS="${linger_seconds}" \
%{ for k, v in env ~}
  -e ${k}='${replace(v, "'", "'\\''")}' \
%{ endfor ~}
  ${worker_image} python -m sluice_worker.vm_agent

# 5. Self-DELETE on idle (the agent returned ⇒ drained past the linger window). A guest `shutdown`
#    only STOPs a GCE instance (its disk keeps billing), so delete the instance outright via its
#    ATTACHED service account (needs compute.instances.delete). Fall back to power-off if the delete
#    can't run (missing IAM / no gcloud) — the autoscaler reconcile then reaps the STOPPED instance.
META="http://metadata.google.internal/computeMetadata/v1/instance"
SELF_NAME="$(curl -s -H 'Metadata-Flavor: Google' "$META/name")"
SELF_ZONE="$(curl -s -H 'Metadata-Flavor: Google' "$META/zone" | awk -F/ '{print $NF}')"
gcloud compute instances delete "$SELF_NAME" --zone="$SELF_ZONE" --quiet || shutdown -h now
