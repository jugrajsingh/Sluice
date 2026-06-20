from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_should_set_use_and_get_contexts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    set_args = ["config", "set-context", "prod", "--api", "https://prod", "--api-key", "K"]
    assert runner.invoke(app, set_args).exit_code == 0
    assert runner.invoke(app, ["config", "use-context", "prod"]).exit_code == 0
    r = runner.invoke(app, ["config", "get-contexts"])
    assert r.exit_code == 0 and "prod" in r.output and "https://prod" in r.output and "*" in r.output


def test_should_say_none_when_no_contexts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    r = runner.invoke(app, ["config", "get-contexts"])
    assert r.exit_code == 0 and "no contexts" in r.output


def test_should_expose_completion_in_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "completion" in r.output.lower()
