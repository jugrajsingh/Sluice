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
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    gpu: int = 0
    gpu_type: str = Field("", alias="gpuType")
    cpu: float = 1.0
    memory_gb: float = Field(2.0, alias="memoryGb")


class Toleration(BaseModel):
    """A Kubernetes toleration synthesized onto worker pods (e.g. to tolerate the GPU taint)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
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

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    pricing: Literal["spot", "on-demand"] = "spot"
    node_selectors: list[dict[str, str]] = Field(default_factory=lambda: [{}], alias="nodeSelectors")
    tolerations: list[Toleration] = Field(default_factory=list)


class VmPlacementSpec(BaseModel):
    """Placement on burst VMs of one cloud. The cloud (`gce`/`ec2`) lives on the candidate."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    pricing: Literal["spot", "on-demand"] = "spot"
    machine_type: str = Field("", alias="machineType")
    accelerator_type: str = Field("", alias="acceleratorType")
    boot_image: str = Field("", alias="bootImage")
    regions: list[str] = Field(default_factory=list)
    linger_seconds: int = Field(300, alias="lingerSeconds")


class ServerSpec(BaseModel):
    """How the sidecar adapter talks to the model server packed in the same unit.

    The adapter is model-agnostic: the queue body is POSTed verbatim and the response is stored
    verbatim. Only these knobs configure it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    port: int = 8080
    request_path: str = Field("/", alias="requestPath")
    method: str = "POST"
    content_type: str = Field("application/octet-stream", alias="contentType")
    health_path: str = Field("/healthz", alias="healthPath")
    ready_timeout_s: int = Field(600, alias="readyTimeoutS")  # cold-start budget before the unit is failed
    concurrency: int = 0  # 0 => match WorkerSpec.instances; else explicit in-flight cap


class WorkerSpec(BaseModel):
    """Worker archetype + packing for an app.

    - `handler`: the model runs in-process (`BaseHandler`); `instances` worker.run processes are
      started **sequentially** in one unit, each leasing independently.
    - `sidecar`: an HTTP model server (packing itself to `instances`) is fed by the Sluice adapter.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    type: Literal["handler", "sidecar"] = "handler"
    instances: int = 1
    args: list[str] = Field(default_factory=list)
    server: ServerSpec | None = None

    @model_validator(mode="after")
    def _sidecar_needs_server(self) -> WorkerSpec:
        if self.type == "sidecar" and self.server is None:
            raise ValueError("worker.type 'sidecar' requires a server config")
        return self


class CandidateOverrides(BaseModel):
    """Per-candidate overrides merged over the app-level values (for heterogeneous GPUs)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    image: str | None = None
    env: dict[str, str] | None = None
    args: list[str] | None = None
    instances: int | None = None


class KubernetesCandidate(BaseModel):
    """A Kubernetes placement candidate.

    `provider` selects the cluster: `in-cluster` (the autoscaler's own cluster) or a name
    registered in the deployment cluster registry (a mounted kubeconfig).
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    type: Literal["kubernetes"] = "kubernetes"
    provider: str = "in-cluster"
    spec: K8sPlacementSpec = Field(default_factory=K8sPlacementSpec)
    overrides: CandidateOverrides | None = None


class VmCandidate(BaseModel):
    """A burst-VM placement candidate. `provider` selects the cloud: `gce` or `ec2`."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    type: Literal["vm"] = "vm"
    provider: str = "gce"
    spec: VmPlacementSpec = Field(default_factory=VmPlacementSpec)
    overrides: CandidateOverrides | None = None


# Discriminated on `type`; the placement list's order IS the priority (per-app).
PlacementCandidate = Annotated[KubernetesCandidate | VmCandidate, Field(discriminator="type")]


def _default_placement() -> list[KubernetesCandidate]:
    """Default placement: one in-cluster spot candidate that schedules anywhere."""
    return [KubernetesCandidate()]


class BatchSpec(BaseModel):
    """Optional bulk-batch config block for an app."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    batch_sla_hours: int = Field(24, alias="batchSlaHours", ge=1)
    output_partition_size: int = Field(1000, alias="outputPartitionSize", ge=1)
    upload_ttl_hours: int = Field(24, alias="uploadTtlHours", ge=1)
    starve_grace_min: int = Field(7, alias="starveGraceMin", ge=1)


class ScalingSpec(BaseModel):
    # One instance = 1 pod = 1 burst VM = one serving unit (each may pack `worker.instances` replicas
    # internally; messagesPerInstance is tuned per packed unit).
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    messages_per_instance: int = Field(10, alias="messagesPerInstance", ge=1)  # queue depth one instance absorbs
    min_instances: int = Field(0, alias="minInstances", ge=0)  # warm floor: always keep ≥ this many serving
    max_instances: int = Field(0, alias="maxInstances", ge=0)  # hard ceiling across pods+VMs; 0 = unbounded
    max_scale_up_per_cycle: int = Field(3, alias="maxScaleUpPerCycle", ge=1)  # cap new units created per reconcile
    scale_up_cooldown_s: int = Field(60, alias="scaleUpCooldownSeconds", ge=0)  # debounce after a scale-up
    scale_down_stabilization_s: int = Field(
        120, alias="scaleDownStabilizationSeconds", ge=0
    )  # hold the recent desired peak this long before shrinking (anti-flap)
    startup_grace_s: int = Field(300, alias="startupGraceSeconds", ge=1)  # pod-schedule + VM-boot deadline
    infer_sla_minutes: int = Field(30, alias="inferSlaMinutes", ge=1)
    rate_per_instance_per_min: int = Field(1000, alias="ratePerInstancePerMin", ge=1)
    put_concurrency: int = Field(8, alias="putConcurrency", ge=1)
    # Hung-VM detection + escalation (ADR-012). A RUNNING VM whose gateway-stamped heartbeat is stale is
    # excluded from capacity (→ a replacement is provisioned), then reset, then deleted — so a hung VM
    # never silently holds a GPU forever. `wedged_restart_max` caps warm-restart attempts before a
    # crash-looping (e.g. OOM) VM is excluded + flagged for the operator.
    vm_heartbeat_stale_seconds: int = Field(180, alias="vmHeartbeatStaleSeconds", ge=30)
    vm_reset_after_seconds: int = Field(600, alias="vmResetAfterSeconds", ge=60)
    vm_delete_after_seconds: int = Field(1200, alias="vmDeleteAfterSeconds", ge=120)
    wedged_restart_max: int = Field(3, alias="wedgedRestartMax", ge=1)

    @model_validator(mode="after")
    def _floor_within_ceiling(self) -> ScalingSpec:
        if self.max_instances > 0 and self.min_instances > self.max_instances:
            raise ValueError("scaling.minInstances must not exceed scaling.maxInstances")
        return self


class AppSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    name: str
    image: str = ""
    handler: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    desired_state: Literal["Ready", "Paused"] = Field("Ready", alias="desiredState")
    queue_ref: str = Field("", alias="queueRef")
    storage_prefix: str = Field("", alias="storagePrefix")
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    scaling: ScalingSpec = Field(default_factory=ScalingSpec)
    worker: WorkerSpec = Field(default_factory=WorkerSpec)
    placement: list[PlacementCandidate] = Field(default_factory=_default_placement)
    batch: BatchSpec | None = Field(default=None)

    @model_validator(mode="after")
    def _defaults_from_name(self) -> AppSpec:
        if not self.queue_ref:
            self.queue_ref = self.name
        if not self.storage_prefix:
            self.storage_prefix = f"AppData/{self.name}"
        return self

    @property
    def infer_queue_ref(self) -> str:
        return f"{self.queue_ref}-infer"

    @property
    def batch_queue_ref(self) -> str:
        return f"{self.queue_ref}-batch"


class AppStatus(BaseModel):
    phase: str = "Ready"  # Ready | Scaling | Held | Paused | Draining
    reason: str | None = None
    candidate: str | None = None  # active placement candidate key
    workers: dict[str, int] = Field(default_factory=dict)
    queue: QueueDepth = Field(default_factory=QueueDepth)
    updated_at: float = 0.0  # wall clock (time.time()) the controller last wrote this; 0 ⇒ never/unknown (stale)


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
    zone: str = ""  # GCE zone (region+suffix); empty for EC2. Needed to address the instance for delete/reset.
    pricing: str  # spot | on-demand
    machine_type: str
    state: VmState = VmState.provisioning
    created_at: float = 0.0  # wall clock (time.time())
    last_heartbeat: float | None = None  # gateway-stamped heartbeat receive-time (hung-VM detection, ADR-012)
