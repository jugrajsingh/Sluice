from __future__ import annotations

from enum import StrEnum
from typing import Literal

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


class NodePoolSpec(BaseModel):
    pricing: Literal["spot", "on-demand"] = "spot"
    selector: dict[str, str] = Field(default_factory=dict)
    zones: list[str] = Field(default_factory=list)


class VmPlacementSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    provider: Literal["gce", "ec2"] = "gce"
    machine_type: str = Field("", alias="machineType")
    accelerator_type: str = Field("", alias="acceleratorType")
    boot_image: str = Field("", alias="bootImage")
    regions: list[str] = Field(default_factory=list)
    workers_per_vm: int = Field(1, alias="workersPerVm")
    linger_seconds: int = Field(300, alias="lingerSeconds")
    max_vms: int = Field(5, alias="maxVms")


class PlacementSpec(BaseModel):
    mode: Literal["kubernetes", "vm", "both"] = "kubernetes"
    pricing: list[Literal["spot", "on-demand"]] = Field(default_factory=lambda: ["spot"])
    kubernetes: list[NodePoolSpec] = Field(default_factory=list)
    vm: VmPlacementSpec | None = None


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
    placement: PlacementSpec = Field(default_factory=PlacementSpec)

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
