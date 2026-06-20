import httpx
from sluice_cli import __version__
from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _env(monkeypatch, handler):
    import sluice_cli.main as mod

    monkeypatch.setattr(mod, "_transport", lambda: httpx.MockTransport(handler))


def test_should_print_version_when_version_flag():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and __version__ in r.stdout


def test_should_render_help_with_exit_zero():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "Sluice" in r.output


def test_should_print_client_and_server_version(monkeypatch):
    _env(monkeypatch, lambda r: httpx.Response(200, json={"version": "9.9.9"}))
    r = runner.invoke(app, ["--api", "http://c", "--api-key", "K", "version"])
    assert r.exit_code == 0 and __version__ in r.output and "9.9.9" in r.output


def test_should_show_unreachable_when_server_down(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("down")

    _env(monkeypatch, boom)
    r = runner.invoke(app, ["--api", "http://c", "version"])
    assert r.exit_code == 0 and "unreachable" in r.output
