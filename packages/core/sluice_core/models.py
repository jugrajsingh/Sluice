from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Message(BaseModel):
    id: str
    body: bytes
    attributes: dict[str, str] = Field(default_factory=dict)
    ack_token: str = ""
    receive_count: int = 1


class QueueDepth(BaseModel):
    visible: int = 0
    in_flight: int = 0
    delayed: int = 0


class WorkerState(StrEnum):
    starting = "starting"  # ContainerCreating / init / model load
    running = "running"  # Running AND health/readiness green
    pending = "pending"  # accepted, awaiting schedule
    unschedulable = "unschedulable"  # cannot place — see reason
    unhealthy = "unhealthy"  # Running but readiness failing / CrashLoopBackOff
    exited = "exited"  # Succeeded — drained and self-terminated
    failed = "failed"  # Failed / OOMKilled / Error


class WorkerStatus(BaseModel):
    pod: str
    state: WorkerState
    reason: str | None = None
    node: str | None = None
    age_s: int = 0
    restarts: int = 0
    candidate: str | None = None  # candidate key this worker was placed on (from pod annotation)


class ResourcesSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    gpu: int = 0
    gpu_type: str = Field("", alias="gpuType")
    cpu: float = 1
    memory_gb: float = Field(2, alias="memoryGb")


class Toleration(BaseModel):
    """A Kubernetes toleration synthesized onto worker pods (e.g. to tolerate the GPU taint)."""

    model_config = ConfigDict(populate_by_name=True)
    key: str = ""
    operator: Literal["Exists", "Equal"] = "Exists"
    value: str = ""
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] = "NoSchedule"


class K8sPlacementSpec(BaseModel):
    """Placement on one Kubernetes cluster.

    `node_selectors` is an ordered list: the controller tries the first selector (e.g. a
    targeted GPU spot pool), and on stockout falls back to the next (e.g. a broader label).
    `tolerations` are synthesized onto the pod so it can land on tainted (GPU) nodes.
    """

    model_config = ConfigDict(populate_by_name=True)
    pricing: Literal["spot", "on-demand"] = "spot"
    node_selectors: list[dict[str, str]] = Field(default_factory=lambda: [{}], alias="nodeSelectors")
    tolerations: list[Toleration] = Field(default_factory=list)
    schedule_grace_s: int = Field(180, alias="scheduleGraceSeconds")


class VmPlacementSpec(BaseModel):
    """Placement on burst VMs of one cloud. The cloud (`gce`/`ec2`) lives on the candidate."""

    model_config = ConfigDict(populate_by_name=True)
    pricing: Literal["spot", "on-demand"] = "spot"
    machine_type: str = Field("", alias="machineType")
    accelerator_type: str = Field("", alias="acceleratorType")
    boot_image: str = Field("", alias="bootImage")
    regions: list[str] = Field(default_factory=list)
    workers_per_vm: int = Field(1, alias="workersPerVm")
    linger_seconds: int = Field(300, alias="lingerSeconds")
    max_vms: int = Field(5, alias="maxVms")


class KubernetesCandidate(BaseModel):
    """A Kubernetes placement candidate.

    `provider` selects the cluster: `in-cluster` (the autoscaler's own cluster) or a name
    registered in the deployment cluster registry (a mounted kubeconfig).
    """

    model_config = ConfigDict(populate_by_name=True)
    type: Literal["kubernetes"] = "kubernetes"
    provider: str = "in-cluster"
    spec: K8sPlacementSpec = Field(default_factory=K8sPlacementSpec)


class VmCandidate(BaseModel):
    """A burst-VM placement candidate. `provider` selects the cloud: `gce` or `ec2`."""

    model_config = ConfigDict(populate_by_name=True)
    type: Literal["vm"] = "vm"
    provider: str = "gce"
    spec: VmPlacementSpec = Field(default_factory=VmPlacementSpec)


# Discriminated on `type`; the placement list's order IS the priority (per-app).
PlacementCandidate = Annotated[KubernetesCandidate | VmCandidate, Field(discriminator="type")]


def _default_placement() -> list[KubernetesCandidate]:
    """Default placement: one in-cluster spot candidate that schedules anywhere."""
    return [KubernetesCandidate()]


class ScalingSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    messages_per_worker: int = Field(10, alias="messagesPerWorker")
    max_workers: int = Field(0, alias="maxWorkers")  # 0 = unbounded
    scale_up_count: int = Field(3, alias="scaleUpCount")
    cooldown_s: int = Field(30, alias="cooldownSeconds")
    schedule_grace_s: int = Field(180, alias="scheduleGraceSeconds")


class AppSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    image: str = ""
    handler: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    desired_state: Literal["Ready", "Paused"] = Field("Ready", alias="desiredState")
    queue_ref: str = Field("", alias="queueRef")
    storage_prefix: str = Field("", alias="storagePrefix")
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    scaling: ScalingSpec = Field(default_factory=ScalingSpec)
    placement: list[PlacementCandidate] = Field(default_factory=_default_placement)

    @model_validator(mode="after")
    def _defaults_from_name(self) -> AppSpec:
        if not self.queue_ref:
            self.queue_ref = self.name
        if not self.storage_prefix:
            self.storage_prefix = f"apps/{self.name}"
        return self


class AppStatus(BaseModel):
    phase: str = "Ready"  # Ready | Scaling | Held | Paused | Draining
    reason: str | None = None
    candidate: str | None = None  # active placement candidate key
    workers: dict[str, int] = Field(default_factory=dict)
    queue: QueueDepth = Field(default_factory=QueueDepth)


class VmState(StrEnum):
    provisioning = "provisioning"
    booting = "booting"
    running = "running"
    stopped = "stopped"
    preempted = "preempted"
    gone = "gone"


class ProvisionError(StrEnum):
    STOCKOUT = "stockout"
    QUOTA = "quota"
    AUTH = "auth"
    OTHER = "other"


class VmRecord(BaseModel):
    id: str
    app: str
    provider: str  # gce | ec2
    region: str
    pricing: str  # spot | on-demand
    machine_type: str
    state: VmState = VmState.provisioning
    created_at: float = 0.0  # wall clock (time.time())
    last_heartbeat: float | None = None
