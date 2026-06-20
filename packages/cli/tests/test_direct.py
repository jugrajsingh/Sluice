import sys

import pytest
import sluice_cli.direct as direct
from sluice_cli.client import CliError
from sluice_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()
GOOD = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: m}\nspec: {image: r/x:1}\n"


def test_should_call_direct_write_without_network(tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text(GOOD)
    captured = {}
    monkeypatch.setattr(direct, "write", lambda text: captured.update(text=text))
    r = runner.invoke(app, ["apply", "-f", str(f), "--direct"])
    assert r.exit_code == 0 and captured["text"] == GOOD and "(direct)" in r.output


def test_should_error_friendly_when_extra_missing(tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text(GOOD)
    monkeypatch.setattr(direct, "write", lambda text: (_ for _ in ()).throw(CliError("install 'sluice-cli[direct]'")))
    r = runner.invoke(app, ["apply", "-f", str(f), "--direct"])
    assert r.exit_code == 1 and "sluice-cli[direct]" in r.output


def test_write_translates_missing_drivers_to_cli_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sluice_drivers.factory", None)
    with pytest.raises(CliError, match="direct"):
        direct.write(GOOD)
