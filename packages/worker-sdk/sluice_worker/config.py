from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    app: str = "app"
    broker_url: str = "http://sluice-gateway"
    broker_token: str = ""
    batch_size: int = 8
    max_jobs: int = 5000
    max_blank_retries: int = 3
    heartbeat_s: int = 50
    ready_marker: str = ""  # if set, the worker touches this file after load() (launcher gating)
    # Sidecar adapter (WORKER__CONCURRENCY, WORKER__SERVER_PORT, WORKER__SERVER_REQUEST_PATH, ...).
    concurrency: int = 1
    server_port: int = 8080
    server_request_path: str = "/"
    server_method: str = "POST"
    server_content_type: str = "application/octet-stream"
    server_health_path: str = "/healthz"
    server_ready_timeout_s: int = 600
    # --- batch lane (spec §3.3/§3.4; WORKER__BATCH_ENABLED, WORKER__PUT_CONCURRENCY, ...) ---
    # When batch_enabled is true, the adapter constructs the dual-source batch lane in _amain:
    # it leases {app}-batch files through the broker, fetches input via the presigned body_url,
    # writes output via broker-minted presigned PUTs, and proxies status through the broker —
    # no object-store credentials on the worker (ADR-002/008).
    batch_enabled: bool = False
    put_concurrency: int = 8  # shared in-flight model-call budget (semaphore)
    batch_output_partition_size: int = 1000  # output records per flushed part
    starve_grace_s: float = 420.0  # anti-starvation floor trigger (7 min)
    batch_heartbeat_s: float = 30.0  # how often to extend the held batch file lease
    infer_presence_poll_s: float = 15.0  # infer-presence refresh cadence
    model_config = SettingsConfigDict(env_prefix="WORKER__", env_nested_delimiter="__", extra="ignore")
