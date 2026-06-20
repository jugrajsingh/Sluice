from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def default_config_path() -> Path:
    """Resolve the config path fresh (honors XDG_CONFIG_HOME at call time)."""
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "sluice" / "config.yaml"


DEFAULT_PATH = default_config_path()
_DEFAULT_API = "http://localhost:8080"


@dataclass
class Resolved:
    api: str
    api_key: str | None


def load_config(path: Path) -> dict:
    if not path.exists():
        return {"contexts": {}}
    return yaml.safe_load(path.read_text()) or {"contexts": {}}


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    path.chmod(0o600)


def _ctx(cfg: dict, name: str | None) -> dict:
    name = name or cfg.get("current-context")
    return (cfg.get("contexts") or {}).get(name, {}) if name else {}


def resolve(*, api: str | None, api_key: str | None, context: str | None, path: Path = DEFAULT_PATH) -> Resolved:
    cfg = load_config(path)
    ctx = _ctx(cfg, context)
    key = api_key or ctx.get("api_key") or (os.environ.get(ctx["api_key_env"]) if ctx.get("api_key_env") else None)
    return Resolved(api=api or ctx.get("api") or _DEFAULT_API, api_key=key)


def use_context(path: Path, name: str) -> None:
    cfg = load_config(path)
    cfg["current-context"] = name
    save_config(path, cfg)


def set_context(path: Path, name: str, *, api: str, api_key: str | None) -> None:
    cfg = load_config(path)
    cfg.setdefault("contexts", {})[name] = {"api": api, **({"api_key": api_key} if api_key else {})}
    save_config(path, cfg)
