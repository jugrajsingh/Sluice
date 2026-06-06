#!/bin/bash
set -e
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e VM_ID="$(hostname)" \
  -e SLUICE_APP="${app}" \
  -e WORKER_IMAGE="${worker_image}" \
  -e WORKERS_PER_VM="${workers_per_vm}" \
  -e LINGER_SECONDS="${linger_seconds}" \
%{ for k, v in env ~}
  -e ${k}="${v}" \
%{ endfor ~}
  ${worker_image} python -m sluice_worker.vm_agent
shutdown -h now
