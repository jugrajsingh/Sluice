from __future__ import annotations

import typer

from ..client import CliError
from ..errors import fail


def register(app: typer.Typer, client_factory) -> None:
    @app.command()
    def logs(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="app name"),
        worker: str = typer.Option(None, "--worker", help="specific worker pod (default: the active one)"),
        since: int = typer.Option(None, "--since", help="only logs newer than N seconds"),
        tail: int = typer.Option(200, "--tail", help="lines from the end of the log"),
        follow: bool = typer.Option(False, "-f", "--follow", help="stream new log output"),
    ) -> None:
        """Show worker logs (k8s pods). VM-backed workers don't ship logs via the API."""
        client = client_factory(ctx)
        try:
            for chunk in client.stream_logs(name, worker=worker, since=since, tail=tail, follow=follow):
                typer.echo(chunk.decode(errors="replace"), nl=False)
        except CliError as e:
            fail(ctx, e)
