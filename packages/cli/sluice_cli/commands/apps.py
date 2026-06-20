from __future__ import annotations

import typer

from ..client import CliError
from ..errors import fail
from ..output import render


def _run(ctx: typer.Context, client_factory, fn) -> None:
    """Invoke a client call, translating CliError into a clean non-zero exit."""
    try:
        fn(client_factory(ctx))
    except CliError as e:
        fail(ctx, e)


def register(app: typer.Typer, client_factory) -> None:
    @app.command()
    def get(ctx: typer.Context, name: str = typer.Argument(None, help="app name; omit to list all")) -> None:
        """List apps, or show one app."""

        def call(client) -> None:
            data = client.get_app(name) if name else client.list_apps()
            render(data, ctx.obj["output"])

        _run(ctx, client_factory, call)

    @app.command()
    def describe(ctx: typer.Context, name: str = typer.Argument(..., help="app name")) -> None:
        """Show full app detail, including per-worker status."""
        _run(ctx, client_factory, lambda client: render(client.get_app(name), ctx.obj["output"]))

    @app.command()
    def delete(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="app name"),
        yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
    ) -> None:
        """Delete an app."""
        if not yes:
            typer.confirm(f"Delete app {name!r}?", abort=True)
        _run(ctx, client_factory, lambda client: client.delete(name))
        typer.secho(f"deleted {name}", fg="green")

    @app.command()
    def pause(ctx: typer.Context, name: str = typer.Argument(..., help="app name")) -> None:
        """Pause an app (stop scaling; keep the spec)."""
        _run(ctx, client_factory, lambda client: client.lifecycle(name, "pause"))
        typer.secho(f"paused {name}", fg="green")

    @app.command()
    def resume(ctx: typer.Context, name: str = typer.Argument(..., help="app name")) -> None:
        """Resume a paused app."""
        _run(ctx, client_factory, lambda client: client.lifecycle(name, "resume"))
        typer.secho(f"resumed {name}", fg="green")

    @app.command()
    def drain(ctx: typer.Context, name: str = typer.Argument(..., help="app name")) -> None:
        """Drain an app (stop new work, let in-flight finish)."""
        _run(ctx, client_factory, lambda client: client.lifecycle(name, "drain"))
        typer.secho(f"draining {name}", fg="green")
