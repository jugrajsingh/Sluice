from __future__ import annotations

from typing import NoReturn

import typer

from .client import CliError


def fail(ctx: typer.Context, err: CliError) -> NoReturn:
    """Print a user-facing CliError and exit 1.

    With ``--verbose`` (stashed on ``ctx.obj``), also print the underlying cause (e.g. the raw
    httpx error) so power users can debug; otherwise just the one-line friendly message.
    """
    typer.secho(str(err), fg="red", err=True)
    if ctx.obj and ctx.obj.get("verbose") and err.__cause__ is not None:
        typer.secho(f"  caused by: {err.__cause__!r}", fg="yellow", err=True)
    raise typer.Exit(1)
