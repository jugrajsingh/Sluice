from __future__ import annotations

import os

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

SETTINGS_YAML = os.getenv("SETTINGS_YAML", "local.env.yaml")


class QueueSettings(BaseModel):
    backend: str = "memory"  # memory | redis | sqs | nats | pubsub
    options: dict[str, str] = Field(default_factory=dict)


class ObjectStoreSettings(BaseModel):
    backend: str = "local"  # local | s3 | gcs | minio
    options: dict[str, str] = Field(default_factory=dict)


class RegistrySettings(BaseModel):
    backend: str = "objectstore"  # objectstore | memory
    options: dict[str, str] = Field(default_factory=dict)


class CacheSettings(BaseModel):
    backend: str = "memory"  # memory | redis | objectstore
    options: dict[str, str] = Field(default_factory=dict)


class PlacementSettings(BaseModel):
    stockout_ttl_s: int = 600
    boot_deadline_s: int = 600
    tf_module_dir: str = "infra/terraform/modules"
    tf_work_root: str = "/tmp/sluice-tf"
    tf_state_backend: dict[str, str] = Field(default_factory=dict)  # {type,bucket,region|prefix}
    provider_defaults: dict[str, str] = Field(default_factory=dict)  # {project,zone_suffix,iam_instance_profile}


class Settings(BaseSettings):
    queue: QueueSettings = Field(default_factory=QueueSettings)
    object_store: ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    placement: PlacementSettings = Field(default_factory=PlacementSettings)

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        yaml_file=SETTINGS_YAML,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_file = os.getenv("SETTINGS_YAML", "local.env.yaml")
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file))
