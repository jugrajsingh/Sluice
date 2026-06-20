import httpx
from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _env(monkeypatch, handler):
    import sluice_cli.main as mod

    monkeypatch.setattr(mod, "_transport", lambda: httpx.MockTransport(handler))


def test_should_stream_logs_and_forward_params(monkeypatch):
    seen = {}

    def h(req):
        seen["path"] = req.url.path
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, content=b"l1\nl2\n")

    _env(monkeypatch, h)
    r = runner.invoke(
        app, ["--api", "http://c", "--api-key", "K", "logs", "m", "--worker", "p1", "--since", "60", "-f"]
    )
    assert r.exit_code == 0 and "l1" in r.output and "l2" in r.output
    assert seen["path"] == "/v1/apps/m/logs"
    assert seen["q"]["worker"] == "p1" and seen["q"]["since"] == "60" and seen["q"]["follow"] == "true"


def test_should_translate_400_to_friendly_error(monkeypatch):
    _env(monkeypatch, lambda r: httpx.Response(400, text="no worker pods for app 'm'"))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "logs", "m"])
    assert r.exit_code == 1 and "no worker pods" in r.output
