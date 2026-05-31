from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    app: str = "app"
    source: str = "jobs"
    batch_size: int = 8
    wait_seconds: int = 10
    max_jobs: int = 5000
    max_blank_retries: int = 3
    lease_seconds: int = 600
    model_config = SettingsConfigDict(env_prefix="WORKER__", env_nested_delimiter="__", extra="ignore")
