"""Kubernetes wiring for the Sluice autoscaler.

Concrete implementations of the `sluice_core` interfaces, built on
`kubernetes_asyncio`:

- `KubePodManager` -> synthesizes bare worker pods directly from the AppSpec
  (image/resources/env) and reaps exited/failed pods (filtered by an ownership
  label — never touches pods owned by anything else).
- `KubeClusterInspector` -> `ClusterInspector`; lists owned pods and maps each to a
  `WorkerState` via `map_pod_state`, deriving age from `creationTimestamp`.

Workers run in a single configured namespace; app names are flat (no per-app
namespaces). The spec store (AppRegistry) lives outside Kubernetes entirely.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from uuid import uuid4

from kubernetes_asyncio import config
from kubernetes_asyncio.client import (
    ApiClient,
    CoreV1Api,
    V1Container,
    V1DeleteOptions,
    V1EnvVar,
    V1HTTPGetAction,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1Probe,
    V1ResourceRequirements,
    V1Toleration,
)
from sluice_core.auth import mint_worker_token
from sluice_core.interfaces import NoWorkerPods
from sluice_core.models import AppSpec, ServerSpec, Toleration, WorkerState, WorkerStatus

from .inspector import map_pod_state

MANAGED_BY_LABEL_KEY = "app.kubernetes.io/managed-by"
APP_LABEL_KEY = "app.kubernetes.io/name"
MANAGED_BY = "sluice"
CANDIDATE_ANNOTATION = "sluice.jugraj.dev/candidate"
GPU_RESOURCE = "nvidia.com/gpu"
_DEFAULT_GPU_TOLERATION = Toleration(key=GPU_RESOURCE, operator="Exists", effect="NoSchedule")

_REAP_STATES = {WorkerState.exited, WorkerState.failed}


def _effective_tolerations(app: AppSpec, tolerations: list[Toleration]) -> list[Toleration]:
    """Author tolerations, plus a default GPU-taint toleration for GPU apps that lack one.

    GPU nodes are tainted (`nvidia.com/gpu`) so non-GPU workloads stay off them; a GPU worker
    must tolerate that taint to schedule. We add it automatically unless the author already
    declared a toleration for the GPU key.
    """
    out = list(tolerations)
    if app.resources.gpu and not any(t.key == GPU_RESOURCE for t in out):
        out.append(_DEFAULT_GPU_TOLERATION)
    return out


def _to_v1_toleration(t: Toleration) -> V1Toleration:
    # An "Exists" toleration must not carry a value.
    return V1Toleration(
        key=t.key or None,
        operator=t.operator,
        value=t.value or None if t.operator == "Equal" else None,
        effect=t.effect or None,
    )


def _env_list(d: dict[str, str]) -> list[V1EnvVar]:
    return [V1EnvVar(name=k, value=v) for k, v in d.items()]


def build_worker_pod(
    app: AppSpec,
    *,
    selector: dict[str, str],
    namespace: str,
    broker_url: str,
    signing_key: str,
    worker_id: str,
    candidate_key: str = "",
    tolerations: list[Toleration] | None = None,
    image: str = "",
    adapter_image: str = "",
    env: dict[str, str] | None = None,
    args: list[str] | None = None,
    instances: int = 1,
    worker_type: str = "handler",
    server: ServerSpec | None = None,
) -> V1Pod:
    """Synthesize a worker pod for either archetype.

    - handler: one container running N worker.run replicas via the sequential launcher (or `run`
      when instances==1); it owns the GPU and carries the broker JWT + model env.
    - sidecar: a model-server container (image's own entrypoint, owns the GPU, model env, NO broker
      creds, startup-probed) + an adapter container that holds the JWT and feeds it over localhost.
    """
    image = image or app.image
    resolved_env = dict(env if env is not None else app.env)
    args = list(args or [])
    token = mint_worker_token(app=app.name, worker_id=worker_id, key=signing_key)
    broker_env = {
        "WORKER__BROKER_URL": broker_url,
        "WORKER__BROKER_TOKEN": token,
        "WORKER__HANDLER": app.handler,
        "WORKER__APP": app.name,
    }
    res = {"cpu": str(app.resources.cpu), "memory": f"{app.resources.memory_gb}Gi"}
    if app.resources.gpu:
        res[GPU_RESOURCE] = str(app.resources.gpu)
    gpu_resources = V1ResourceRequirements(requests=res, limits=res)

    if worker_type == "sidecar":
        if server is None:
            raise ValueError("sidecar worker requires a server spec")
        server_c = V1Container(
            name="server",  # the model's own entrypoint; owns the GPU; holds no broker creds
            image=image,
            args=args or None,
            env=_env_list(resolved_env),
            resources=gpu_resources,
            startup_probe=V1Probe(
                http_get=V1HTTPGetAction(path=server.health_path, port=server.port),
                period_seconds=10,
                failure_threshold=max(1, server.ready_timeout_s // 10),
            ),
        )
        adapter_env = {
            **broker_env,
            "WORKER__CONCURRENCY": str(server.concurrency or instances),
            "WORKER__SERVER_PORT": str(server.port),
            "WORKER__SERVER_REQUEST_PATH": server.request_path,
            "WORKER__SERVER_METHOD": server.method,
            "WORKER__SERVER_CONTENT_TYPE": server.content_type,
            "WORKER__SERVER_HEALTH_PATH": server.health_path,
            "WORKER__SERVER_READY_TIMEOUT_S": str(server.ready_timeout_s),
        }
        adapter_c = V1Container(
            name="worker",
            # the adapter runs in the Sluice worker-base image (sluice_worker); the server keeps the
            # unmodified BYO model image. Falls back to the app image for a combined image.
            image=adapter_image or image,
            command=["python", "-m", "sluice_worker.adapter"],
            env=_env_list(adapter_env),
        )
        containers = [server_c, adapter_c]
    else:  # handler
        command = (
            ["python", "-m", "sluice_worker.launch", "--instances", str(instances)]
            if instances > 1
            else ["python", "-m", "sluice_worker.run"]
        )
        containers = [
            V1Container(
                name="worker",
                image=image,
                command=command,
                env=_env_list({**broker_env, **resolved_env}),
                resources=gpu_resources,
            )
        ]

    annotations = {CANDIDATE_ANNOTATION: candidate_key} if candidate_key else None
    tols = [_to_v1_toleration(t) for t in _effective_tolerations(app, tolerations or [])]
    return V1Pod(
        metadata=V1ObjectMeta(
            generate_name=f"{app.name}-",
            namespace=namespace,
            labels={APP_LABEL_KEY: app.name, MANAGED_BY_LABEL_KEY: MANAGED_BY},
            annotations=annotations,
        ),
        spec=V1PodSpec(
            restart_policy="OnFailure",
            node_selector=selector or None,
            tolerations=tols or None,
            containers=containers,
        ),
    )


class _KubeBase:
    """Shared connection lifecycle for the K8s-backed components."""

    def __init__(
        self,
        *,
        in_cluster: bool = True,
        config_path: str | None = None,
        context_name: str | None = None,
        request_timeout: int = 30,
        namespace: str = "default",
    ) -> None:
        self._in_cluster = in_cluster
        self._config_path = config_path
        self._context_name = context_name
        self._request_timeout = request_timeout
        self._ns = namespace
        self._api_client: ApiClient | None = None
        self._stack = AsyncExitStack()

    async def open(self) -> None:
        if self._api_client is not None:
            return
        if self._in_cluster:
            config.load_incluster_config()
        else:
            await config.load_kube_config(config_file=self._config_path, context=self._context_name)
        self._api_client = await self._stack.enter_async_context(ApiClient())

    async def close(self) -> None:
        await self._stack.aclose()
        self._api_client = None


def _owned_selector(app_name: str) -> str:
    return f"{APP_LABEL_KEY}={app_name},{MANAGED_BY_LABEL_KEY}={MANAGED_BY}"


class KubePodManager(_KubeBase):
    """Synthesizes bare worker pods from the AppSpec; reaps exited/failed pods."""

    def __init__(
        self,
        *,
        max_concurrent_creates: int = 50,
        broker_url: str = "http://sluice-gateway",
        signing_key: str = "",
        worker_base_image: str = "",
        **kw,
    ) -> None:
        super().__init__(**kw)
        self._create_sem = asyncio.Semaphore(max_concurrent_creates)
        self._delete_options = V1DeleteOptions()
        self._broker_url = broker_url
        self._signing_key = signing_key
        self._worker_base_image = worker_base_image  # sidecar adapter image (sluice_worker); server keeps app.image

    def _core(self) -> CoreV1Api:
        assert self._api_client is not None
        return CoreV1Api(self._api_client)

    async def create_pods(
        self,
        app: AppSpec,
        n: int,
        *,
        selector: dict[str, str],
        candidate_key: str = "",
        tolerations: list[Toleration] | None = None,
        image: str = "",
        adapter_image: str = "",
        env: dict[str, str] | None = None,
        args: list[str] | None = None,
        instances: int = 1,
        worker_type: str = "handler",
        server: ServerSpec | None = None,
    ) -> None:
        if n <= 0:
            return

        async def _create() -> None:
            pod = build_worker_pod(
                app,
                selector=selector,
                namespace=self._ns,
                broker_url=self._broker_url,
                signing_key=self._signing_key,
                worker_id=f"{app.name}-{uuid4().hex[:8]}",
                candidate_key=candidate_key,
                tolerations=tolerations,
                image=image,
                adapter_image=adapter_image or self._worker_base_image,
                env=env,
                args=args,
                instances=instances,
                worker_type=worker_type,
                server=server,
            )
            async with self._create_sem:
                await self._core().create_namespaced_pod(
                    namespace=self._ns, body=pod, _request_timeout=self._request_timeout
                )

        await asyncio.gather(*(_create() for _ in range(n)))

    async def delete_pods(self, app: AppSpec, names: list[str]) -> None:
        if not names:
            return
        await asyncio.gather(
            *(
                self._core().delete_namespaced_pod(
                    name=name, namespace=self._ns, body=self._delete_options, _request_timeout=self._request_timeout
                )
                for name in names
            ),
            return_exceptions=True,
        )

    async def reap_exited(self, app: AppSpec, workers: list[WorkerStatus]) -> None:
        await self.delete_pods(app, [w.pod for w in workers if w.state in _REAP_STATES])


def _age_seconds(pod: dict, now: datetime) -> int:
    ts = pod.get("metadata", {}).get("creationTimestamp")
    if not ts:
        return 0
    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return max(int((now - created).total_seconds()), 0)


class KubeClusterInspector(_KubeBase):
    """`ClusterInspector`: lists owned pods and maps each to a `WorkerState`."""

    def _core(self) -> CoreV1Api:
        assert self._api_client is not None
        return CoreV1Api(self._api_client)

    async def workers(self, app: AppSpec) -> list[WorkerStatus]:
        resp = await self._core().list_namespaced_pod(
            namespace=self._ns, label_selector=_owned_selector(app.name), _request_timeout=self._request_timeout
        )
        now = datetime.now(UTC)
        out: list[WorkerStatus] = []
        for pod in resp.to_dict().get("items", []):
            state, reason = map_pod_state(pod)
            meta = pod.get("metadata", {})
            out.append(
                WorkerStatus(
                    pod=meta.get("name", "?"),
                    state=state,
                    reason=reason,
                    node=pod.get("spec", {}).get("nodeName"),
                    age_s=_age_seconds(pod, now),
                    restarts=sum(
                        (cs.get("restartCount", 0) or 0)
                        for cs in pod.get("status", {}).get("containerStatuses", []) or []
                    ),
                    candidate=(meta.get("annotations") or {}).get(CANDIDATE_ANNOTATION),
                )
            )
        return out

    async def _active_pod(self, app: AppSpec) -> str | None:
        """Pick a pod to read logs from: a running worker if any, else any owned pod."""
        workers = await self.workers(app)
        running = [w for w in workers if w.state == WorkerState.running]
        chosen = running or workers
        return chosen[0].pod if chosen else None

    async def pod_logs(
        self,
        app: AppSpec,
        *,
        pod: str | None = None,
        since_seconds: int | None = None,
        tail: int = 200,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        """Stream logs from one worker pod (the active one when `pod` is None).

        Raises `NoWorkerPods` when there is no pod to read from (e.g. a VM-backed app, whose
        workers don't run as k8s pods, or an app scaled to zero).
        """
        target = pod or await self._active_pod(app)
        if target is None:
            raise NoWorkerPods(f"no worker pods for app {app.name!r} (VM workers don't ship logs via the API)")
        resp = await self._core().read_namespaced_pod_log(
            name=target,
            namespace=self._ns,
            since_seconds=since_seconds,
            tail_lines=tail,
            follow=follow,
            timestamps=False,
            _preload_content=False,
        )
        async for chunk in resp.content.iter_any():
            yield chunk
