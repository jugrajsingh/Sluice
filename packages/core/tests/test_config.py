from sluice_core.config import Settings


def test_defaults():
    s = Settings()
    assert s.queue.backend == "memory"
    assert s.object_store.backend == "local"


def test_env_override(monkeypatch):
    monkeypatch.setenv("QUEUE__BACKEND", "redis")
    monkeypatch.setenv("OBJECT_STORE__BACKEND", "s3")
    s = Settings()
    assert s.queue.backend == "redis"
    assert s.object_store.backend == "s3"


def test_yaml_loaded(tmp_path, monkeypatch):
    f = tmp_path / "local.env.yaml"
    f.write_text("queue:\n  backend: nats\n")
    monkeypatch.setenv("SETTINGS_YAML", str(f))
    s = Settings()
    assert s.queue.backend == "nats"


def test_placement_defaults():
    s = Settings()
    assert s.placement.stockout_ttl_s == 600 and s.placement.boot_deadline_s == 600
    assert s.registry.backend == "objectstore" and s.cache.backend == "memory"
