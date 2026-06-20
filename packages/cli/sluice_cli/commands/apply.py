from __future__ import annotations

import difflib
from pathlib import Path

import typer
from sluice_core.app_yaml import serialize_app_yaml

from .. import direct as _direct
from ..client import CliError
from ..errors import fail
from ..spec import load_and_validate


def _print_dry_run(client, name: str, proposed_yaml: str) -> None:
    """Fetch the current stored spec (if any) and show a diff against the proposed one."""
    current = client.get_spec(name)
    if current is None:
        typer.secho(f"ok (dry-run): {name} is new — would be created", fg="yellow")
        return
    diff = list(
        difflib.unified_diff(
            current.splitlines(),
            proposed_yaml.splitlines(),
            fromfile=f"{name} (current)",
            tofile=f"{name} (proposed)",
            lineterm="",
        )
    )
    if not diff:
        typer.secho(f"ok (dry-run): {name} — no changes", fg="yellow")
        return
    typer.secho(f"# dry-run: {name} — proposed changes:", fg="yellow")
    for line in diff:
        color = "green" if line.startswith("+") else "red" if line.startswith("-") else None
        typer.secho(line, fg=color)


def register(app: typer.Typer, client_factory) -> None:
    @app.command()
    def validate(file: str = typer.Option(..., "-f", "--file", help="path to the App YAML spec")) -> None:
        """Strict-validate an App spec locally (no network). Unknown/mistyped fields are reported."""
        _spec, errs = load_and_validate(Path(file).read_text())
        if errs:
            for e in errs:
                typer.secho(e, fg="red", err=True)
            raise typer.Exit(2)
        typer.secho("valid", fg="green")

    @app.command(
        epilog="Examples:\n  sluice apply -f app.yaml\n  sluice apply -f app.yaml --dry-run   # preview a diff",
    )
    def apply(
        ctx: typer.Context,
        file: str = typer.Option(..., "-f", "--file", help="path to the App YAML spec"),
        dry_run: bool = typer.Option(False, "--dry-run", help="validate + show a diff vs the live spec; no write"),
        direct: bool = typer.Option(
            False, "--direct", help="write the spec store directly (bootstrap); needs the optional 'direct' extra"
        ),
    ) -> None:
        """Apply an App spec (strict-validated, then sent to the console)."""
        text = Path(file).read_text()
        spec, errs = load_and_validate(text)
        if errs:
            for e in errs:
                typer.secho(e, fg="red", err=True)
            raise typer.Exit(2)
        assert spec is not None  # invariant: no errors ⇒ a parsed spec
        try:
            if dry_run:
                _print_dry_run(client_factory(ctx), spec.name, serialize_app_yaml(spec))
            elif direct:
                _direct.write(text)
            else:
                client_factory(ctx).apply(spec.name, text)
        except CliError as e:
            fail(ctx, e)
        if not dry_run:
            typer.secho(f"applied {spec.name}{' (direct)' if direct else ''}", fg="green")
