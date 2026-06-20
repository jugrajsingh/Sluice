from sluice_worker.config import WorkerSettings


def test_defaults_and_env(monkeypatch):
    assert WorkerSettings().batch_size == 8
    monkeypatch.setenv("WORKER__MAX_JOBS", "5")
    assert WorkerSettings().max_jobs == 5
    monkeypatch.setenv("WORKER__APP", "tw")
    assert WorkerSettings().app == "tw"


def test_should_default_batch_disabled_when_unset():
    assert WorkerSettings().batch_enabled is False


def test_should_enable_batch_when_env_set(monkeypatch):
    monkeypatch.setenv("WORKER__BATCH_ENABLED", "1")
    s = WorkerSettings()
    assert s.batch_enabled is True
    # batch tuning knobs have spec defaults (§3.3/§3.4)
    assert s.batch_output_partition_size == 1000
    assert s.put_concurrency == 8
