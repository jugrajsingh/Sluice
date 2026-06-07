import json

import httpx
from sluice_cli.main import run

APP_YAML = """
apiVersion: sluice/v1
kind: App
metadata: { name: topwear }
spec: { image: repo/x:1, handler: "h:H" }
"""


def _mock(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "topwear", "scale_status": "ready"}])
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")


def test_apply_puts_yaml(tmp_path):
    f = tmp_path / "app.yaml"
    f.write_text(APP_YAML)
    calls = []
    assert run(["apply", "-f", str(f)], client=_mock(calls)) == 0
    assert calls == [("PUT", "/v1/apps/topwear")]


def test_apply_validates_before_sending(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("apiVersion: nope")
    calls = []
    assert run(["apply", "-f", str(f)], client=_mock(calls)) == 2
    assert calls == []


def test_get_pause_resume_delete():
    calls = []
    c = _mock(calls)
    assert run(["get"], client=c) == 0
    assert run(["pause", "topwear"], client=c) == 0
    assert run(["resume", "topwear"], client=c) == 0
    assert run(["delete", "topwear"], client=c) == 0
    assert ("POST", "/v1/apps/topwear/pause") in calls
    assert ("POST", "/v1/apps/topwear/resume") in calls
    assert ("DELETE", "/v1/apps/topwear") in calls


def test_apply_direct_writes_spec_store(tmp_path, monkeypatch):
    f = tmp_path / "app.yaml"
    f.write_text(APP_YAML)
    monkeypatch.setenv("REGISTRY__BACKEND", "objectstore")
    monkeypatch.setenv("OBJECT_STORE__BACKEND", "local")
    monkeypatch.setenv("OBJECT_STORE__OPTIONS", json.dumps({"root": str(tmp_path / "store")}))
    assert run(["apply", "-f", str(f), "--direct"]) == 0
    assert (tmp_path / "store" / "sluice" / "apps" / "topwear" / "spec.yaml").is_file()
