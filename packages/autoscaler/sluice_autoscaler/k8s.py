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
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from kubernetes_asyncio import config
from kubernetes_asyncio.client import (
    ApiClient,
    CoreV1Api,
    V1Container,
    V1DeleteOptions,
    V1EnvVar,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1ResourceRequirements,
)
from sluice_core.config import Settings
from sluice_core.models import AppSpec, WorkerState, WorkerStatus

from .inspector import map_pod_state

MANAGED_BY_LABEL_KEY = "app.kubernetes.io/managed-by"
APP_LABEL_KEY = "app.kubernetes.io/name"
MANAGED_BY = "sluice"
CANDIDATE_ANNOTATION = "sluice.jugraj.dev/candidate"

_REAP_STATES = {WorkerState.exited, WorkerState.failed}


def backend_env_from_settings(s: Settings) -> dict[str, str]:
    return {
        "QUEUE__BACKEND": s.queue.backend,
        "QUEUE__OPTIONS": json.dumps(s.queue.options),
        "OBJECT_STORE__BACKEND": s.object_store.backend,
        "OBJECT_STORE__OPTIONS": json.dumps(s.object_store.options),
    }


def build_worker_pod(
    app: AppSpec, *, selector: dict[str, str], backend_env: dict[str, str], namespace: str, candidate_key: str = ""
) -> V1Pod:
    env_map = {
        **backend_env,
        "WORKER__HANDLER": app.handler,
        "WORKER__SOURCE": app.queue_ref,
        "WORKER__APP": app.name,
        **app.env,
    }
    env = [V1EnvVar(name=k, value=v) for k, v in env_map.items()]
    res = {"cpu": str(app.resources.cpu), "memory": f"{app.resources.memory_gb}Gi"}
    if app.resources.gpu:
        res["nvidia.com/gpu"] = str(app.resources.gpu)
    container = V1Container(
        name="worker",
        image=app.image,
        command=["python", "-m", "sluice_worker.run"],
        env=env,
        resources=V1ResourceRequirements(requests=res, limits=res),
    )
    annotations = {CANDIDATE_ANNOTATION: candidate_key} if candidate_key else None
    return V1Pod(
        metadata=V1ObjectMeta(
            generate_name=f"{app.name}-",
            namespace=namespace,
            labels={APP_LABEL_KEY: app.name, MANAGED_BY_LABEL_KEY: MANAGED_BY},
            annotations=annotations,
        ),
        spec=V1PodSpec(restart_policy="OnFailure", node_selector=selector or None, containers=[container]),
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

    def __init__(self, *, max_concurrent_creates: int = 50, backend_env: dict[str, str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self._create_sem = asyncio.Semaphore(max_concurrent_creates)
        self._delete_options = V1DeleteOptions()
        self._env = backend_env or {}

    def _core(self) -> CoreV1Api:
        assert self._api_client is not None
        return CoreV1Api(self._api_client)

    async def create_pods(self, app: AppSpec, n: int, *, selector: dict[str, str], candidate_key: str = "") -> None:
        if n <= 0:
            return

        async def _create() -> None:
            pod = build_worker_pod(
                app, selector=selector, backend_env=self._env, namespace=self._ns, candidate_key=candidate_key
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
