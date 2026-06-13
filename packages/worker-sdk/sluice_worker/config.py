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
    # Sidecar adapter (WORKER__CONCURRENCY, WORKER__SERVER_PORT, WORKER__SERVER_REQUEST_PATH, ...).
    concurrency: int = 1
    server_port: int = 8080
    server_request_path: str = "/"
    server_method: str = "POST"
    server_content_type: str = "application/octet-stream"
    server_health_path: str = "/healthz"
    server_ready_timeout_s: int = 600
    model_config = SettingsConfigDict(env_prefix="WORKER__", env_nested_delimiter="__", extra="ignore")
