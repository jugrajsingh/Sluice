import json

import httpx
from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

_APP = {
    "name": "m",
    "desired_state": "Ready",
    "phase": "Ready",
    "reason": None,
    "candidate": None,
    "updated_at": 0.0,
    "scale_status": "ready",
    "queue": {"visible": 3, "in_flight": 1},
    "workers": {"running": 2},
}
_DETAIL = {**_APP, "worker_list": [{"pod": "m-abc", "state": "running", "age_s": 12, "restarts": 0, "node": "n1"}]}
_HELD = {
    **_APP,
    "phase": "Held",
    "reason": "stockout",
    "candidate": "vm/gce/us-central1/spot",
    "updated_at": 1_700_000_000.0,
}


def _env(monkeypatch, handler):
    import sluice_cli.main as mod

    monkeypatch.setattr(mod, "_transport", lambda: httpx.MockTransport(handler))


def test_should_list_apps_as_table(monkeypatch):
    _env(monkeypatch, lambda r: httpx.Response(200, json=[_APP]))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "get"])
    assert r.exit_code == 0 and "m" in r.output and "Ready" in r.output


def test_should_emit_json_when_output_json(monkeypatch):
    _env(monkeypatch, lambda r: httpx.Response(200, json=[_APP]))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "-o", "json", "get"])
    assert r.exit_code == 0 and json.loads(r.output)[0]["name"] == "m"


def test_should_describe_single_app_with_workers(monkeypatch):
    _env(monkeypatch, lambda r: httpx.Response(200, json=_DETAIL))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "describe", "m"])
    assert r.exit_code == 0 and "m-abc" in r.output


def test_should_surface_reason_and_candidate_in_describe(monkeypatch):
    detail = {**_HELD, "worker_list": _DETAIL["worker_list"]}
    _env(monkeypatch, lambda r: httpx.Response(200, json=detail))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "describe", "m"])
    assert r.exit_code == 0
    assert "Held" in r.output  # authoritative phase
    assert "stockout" in r.output  # the "why"
    assert "vm/gce/us-central1/spot" in r.output  # active candidate


def test_should_show_persisted_phase_over_scale_status_hint(monkeypatch):
    # Authoritative phase from the controller takes precedence over the live scale_status hint.
    held_list = {**_HELD, "scale_status": "ready"}
    _env(monkeypatch, lambda r: httpx.Response(200, json=[held_list]))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "get"])
    assert r.exit_code == 0 and "Held" in r.output


def test_should_fall_back_to_scale_status_when_no_phase(monkeypatch):
    # No persisted phase (controller never wrote) ⇒ table shows the live scale_status hint.
    no_phase = {**_APP, "phase": None, "scale_status": "scaling"}
    _env(monkeypatch, lambda r: httpx.Response(200, json=[no_phase]))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "get"])
    assert r.exit_code == 0 and "scaling" in r.output


def test_should_post_pause_with_key(monkeypatch):
    seen = {}
    _env(
        monkeypatch,
        lambda r: seen.update(p=r.url.path, k=r.headers.get("X-API-Key")) or httpx.Response(200, json={}),
    )
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "pause", "m"])
    assert r.exit_code == 0 and seen == {"p": "/v1/apps/m/pause", "k": "K"}


def test_should_require_confirm_before_delete(monkeypatch):
    seen = {"n": 0}
    _env(monkeypatch, lambda r: seen.update(n=seen["n"] + 1) or httpx.Response(200, json={}))
    runner.invoke(app, ["--api", "http://c", "--api-key", "K", "delete", "m"], input="n\n")
    assert seen["n"] == 0  # aborted, no DELETE sent


def test_should_delete_when_yes_flag(monkeypatch):
    seen = {}
    _env(monkeypatch, lambda r: seen.update(m=r.method, p=r.url.path) or httpx.Response(200, json={"deleted": "m"}))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "delete", "m", "--yes"])
    assert r.exit_code == 0 and seen == {"m": "DELETE", "p": "/v1/apps/m"}
