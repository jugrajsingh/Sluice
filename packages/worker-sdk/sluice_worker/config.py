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
    model_config = SettingsConfigDict(env_prefix="WORKER__", env_nested_delimiter="__", extra="ignore")
