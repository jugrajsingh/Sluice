from __future__ import annotations

import httpx
import typer

from . import __version__
from .client import AdminClient
from .commands import apply as _apply
from .commands import apps as _apps
from .commands import config as _config
from .commands import logs as _logs
from .commands import schema as _schema
from .config import resolve

app = typer.Typer(
    no_args_is_help=True,
    add_completion=True,
    help="Sluice control CLI — manage apps on a Sluice cluster.",
    epilog=(
        "sluice talks to the CONSOLE (admin API; set --api / SLUICE_API). "
        "End users send inference to the GATEWAY (POST /v1/<app>/infer) — a different URL.\n\n"
        "Typical flow:  sluice validate -f app.yaml  ->  sluice apply -f app.yaml  ->  "
        "sluice get  ->  sluice logs <app> -f"
    ),
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"sluice {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    api: str = typer.Option(None, "--api", envvar="SLUICE_API", help="Admin API base URL (the console)"),
    api_key: str = typer.Option(None, "--api-key", envvar="SLUICE_API_KEY", help="X-API-Key"),
    context: str = typer.Option(None, "--context", help="Config context to use"),
    output: str = typer.Option("table", "-o", "--output", help="table|json|yaml|wide"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="show underlying errors / more detail"),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    # Stash global options for the command functions (resolved against config below).
    ctx.obj = {"api": api, "api_key": api_key, "context": context, "output": output, "verbose": verbose}


def _transport() -> httpx.BaseTransport | None:  # overridden in tests
    return None


def _client(ctx: typer.Context) -> AdminClient:
    o = ctx.obj
    r = resolve(api=o["api"], api_key=o["api_key"], context=o["context"])
    return AdminClient(r.api, r.api_key, transport=_transport())


@app.command()
def version(ctx: typer.Context) -> None:
    """Show the CLI version and the connected server's version (also a connectivity check)."""
    typer.echo(f"client: {__version__}")
    typer.echo(f"server: {_client(ctx).server_version()}")


_apply.register(app, _client)
_apps.register(app, _client)
_logs.register(app, _client)
_schema.register(app)
_config.register(app)


def main() -> None:
    app()
