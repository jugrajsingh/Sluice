from __future__ import annotations

import typer

from ..config import default_config_path, load_config, set_context, use_context


def register(app: typer.Typer) -> None:
    config_app = typer.Typer(no_args_is_help=True, help="Manage CLI contexts (~/.config/sluice/config.yaml)")
    app.add_typer(config_app, name="config")

    @config_app.command("get-contexts")
    def get_contexts() -> None:
        """List configured contexts (the active one is marked with '*')."""
        cfg = load_config(default_config_path())
        current = cfg.get("current-context")
        contexts = cfg.get("contexts") or {}
        if not contexts:
            typer.echo("no contexts configured — add one with `sluice config set-context`")
            return
        for name, c in contexts.items():
            marker = "*" if name == current else " "
            typer.echo(f"{marker} {name}\t{c.get('api', '')}")

    @config_app.command("use-context")
    def use_context_cmd(name: str = typer.Argument(..., help="context name")) -> None:
        """Set the active context."""
        use_context(default_config_path(), name)
        typer.secho(f"switched to context {name!r}", fg="green")

    @config_app.command("set-context")
    def set_context_cmd(
        name: str = typer.Argument(..., help="context name"),
        api: str = typer.Option(..., "--api", help="admin API base URL (the console)"),
        api_key: str = typer.Option(None, "--api-key", help="X-API-Key (prefer env over a stored literal)"),
    ) -> None:
        """Create or update a context."""
        set_context(default_config_path(), name, api=api, api_key=api_key)
        typer.secho(f"context {name!r} saved", fg="green")
