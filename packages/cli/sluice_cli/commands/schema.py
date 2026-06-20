from __future__ import annotations

import json

import typer
from sluice_core.models import AppSpec


def register(app: typer.Typer) -> None:
    @app.command()
    def schema() -> None:
        """Print the App-spec JSON schema (field names use their YAML aliases)."""
        typer.echo(json.dumps(AppSpec.model_json_schema(by_alias=True), indent=2))
