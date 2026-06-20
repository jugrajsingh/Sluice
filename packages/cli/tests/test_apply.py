import httpx
from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
GOOD = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: m}\nspec: {image: r/x:1}\n"
TYPO = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: m}\nspec: {scaling: {maxInstance: 7}}\n"


def _env(monkeypatch, handler):
    import sluice_cli.main as mod

    monkeypatch.setattr(mod, "_transport", lambda: httpx.MockTransport(handler))


def test_should_strict_validate_and_put_when_apply(tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text(GOOD)
    seen = {}
    _env(
        monkeypatch,
        lambda r: (
            seen.update(m=r.method, p=r.url.path, k=r.headers.get("X-API-Key"))
            or httpx.Response(200, json={"applied": "m"})
        ),
    )
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "apply", "-f", str(f)])
    assert r.exit_code == 0 and seen == {"m": "PUT", "p": "/v1/apps/m", "k": "K"}


def test_should_fail_validate_without_network_on_typo(tmp_path, monkeypatch):
    f = tmp_path / "b.yaml"
    f.write_text(TYPO)
    called = {"n": 0}
    _env(monkeypatch, lambda r: called.update(n=called["n"] + 1) or httpx.Response(200))
    r = runner.invoke(app, ["validate", "-f", str(f)])
    assert r.exit_code != 0 and "maxInstances" in r.output and called["n"] == 0


def test_should_say_new_on_dry_run_when_absent(tmp_path, monkeypatch):
    f = tmp_path / "c.yaml"
    f.write_text(GOOD)
    methods = []

    def h(req):
        methods.append(req.method)
        return httpx.Response(404) if req.url.path.endswith("/spec") else httpx.Response(200, json={})

    _env(monkeypatch, h)
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "apply", "-f", str(f), "--dry-run"])
    assert r.exit_code == 0 and "new" in r.output.lower() and "PUT" not in methods


def test_should_reveal_cause_when_verbose(tmp_path, monkeypatch):
    f = tmp_path / "v.yaml"
    f.write_text(GOOD)

    def boom(req):
        raise httpx.ConnectError("connection refused detail")

    _env(monkeypatch, boom)
    r = runner.invoke(app, ["-v", "--api", "http://c", "--api-key", "K", "apply", "-f", str(f)])
    assert r.exit_code == 1 and "cannot reach" in r.output and "caused by" in r.output and "ConnectError" in r.output


def test_should_hide_cause_without_verbose(tmp_path, monkeypatch):
    f = tmp_path / "v.yaml"
    f.write_text(GOOD)

    def boom(req):
        raise httpx.ConnectError("x")

    _env(monkeypatch, boom)
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "apply", "-f", str(f)])
    assert r.exit_code == 1 and "cannot reach" in r.output and "caused by" not in r.output


def test_should_show_diff_on_dry_run_against_existing(tmp_path, monkeypatch):
    from sluice_core.app_yaml import parse_app_yaml, serialize_app_yaml

    current = serialize_app_yaml(parse_app_yaml(GOOD))  # stored spec has image r/x:1
    f = tmp_path / "c.yaml"
    f.write_text(GOOD.replace("r/x:1", "r/x:2"))  # propose a changed image
    methods = []

    def h(req):
        methods.append(req.method)
        return httpx.Response(200, text=current) if req.url.path.endswith("/spec") else httpx.Response(200, json={})

    _env(monkeypatch, h)
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "apply", "-f", str(f), "--dry-run"])
    assert r.exit_code == 0 and "PUT" not in methods
    assert "dry-run" in r.output and "r/x:2" in r.output and "+" in r.output
