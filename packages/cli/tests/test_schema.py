from sluice_cli.main import app
from typer.testing import CliRunner


def test_should_print_appspec_schema():
    r = CliRunner().invoke(app, ["schema"])
    assert r.exit_code == 0 and "batchSlaHours" in r.output and "placement" in r.output
