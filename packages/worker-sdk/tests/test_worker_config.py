from sluice_worker.config import WorkerSettings


def test_defaults_and_env(monkeypatch):
    assert WorkerSettings().batch_size == 8
    monkeypatch.setenv("WORKER__MAX_JOBS", "5")
    assert WorkerSettings().max_jobs == 5
    monkeypatch.setenv("WORKER__APP", "tw")
    assert WorkerSettings().app == "tw"
