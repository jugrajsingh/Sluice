from __future__ import annotations

import json
import time
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table


def _workers_summary(workers: dict[str, int]) -> str:
    return ",".join(f"{k}:{v}" for k, v in sorted(workers.items())) or "-"


def _queue_summary(queue: dict[str, Any]) -> str:
    return f"{queue.get('visible', 0)}/{queue.get('in_flight', 0)}"  # visible/in-flight


def _phase(a: dict) -> str:
    """The controller's authoritative phase when present; else the live-derived scale_status hint."""
    return a.get("phase") or a.get("scale_status", "")


def _app_table(apps: list[dict], *, wide: bool) -> Table:
    table = Table()
    table.add_column("NAME")
    table.add_column("DESIRED")
    table.add_column("PHASE")
    if wide:
        table.add_column("REASON")
        table.add_column("CANDIDATE")
        table.add_column("QUEUE(v/if)")
        table.add_column("WORKERS")
    for a in apps:
        row = [a.get("name", ""), a.get("desired_state", ""), _phase(a)]
        if wide:
            row += [
                a.get("reason") or "-",
                a.get("candidate") or "-",
                _queue_summary(a.get("queue", {})),
                _workers_summary(a.get("workers", {})),
            ]
        table.add_row(*row)
    return table


def _as_of(updated_at: float) -> str:
    """Human staleness for the persisted-status timestamp; '-' when never written (0)."""
    if not updated_at:
        return "-"
    age = max(0, int(time.time() - updated_at))
    return f"{age}s ago"


def _status_detail(a: dict) -> Table:
    """The controller's authoritative verdict for a single app: phase / reason / candidate + staleness."""
    table = Table(title="status", show_header=False)
    table.add_column("FIELD")
    table.add_column("VALUE")
    table.add_row("phase", _phase(a))
    table.add_row("reason", a.get("reason") or "-")
    table.add_row("candidate", a.get("candidate") or "-")
    table.add_row("as of", _as_of(a.get("updated_at", 0.0)))
    return table


def _worker_table(workers: list[dict]) -> Table:
    table = Table(title="workers")
    for col in ("POD", "STATE", "AGE(s)", "RESTARTS", "NODE", "REASON"):
        table.add_column(col)
    for w in workers:
        table.add_row(
            w.get("pod", ""),
            str(w.get("state", "")),
            str(w.get("age_s", "")),
            str(w.get("restarts", "")),
            w.get("node") or "-",
            w.get("reason") or "-",
        )
    return table


def render(data: list[dict] | dict, fmt: str) -> None:
    """Render API data in the requested format. `fmt` is one of table|wide|json|yaml."""
    if fmt == "json":
        typer.echo(json.dumps(data, indent=2))
        return
    if fmt == "yaml":
        typer.echo(yaml.safe_dump(data, sort_keys=False).rstrip())
        return
    wide = fmt == "wide"
    console = Console()
    if isinstance(data, list):
        console.print(_app_table(data, wide=wide))
        return
    # single app (detail): summary row + the controller's status verdict + per-worker table when present
    console.print(_app_table([data], wide=wide))
    console.print(_status_detail(data))
    workers = data.get("worker_list")
    if workers:
        console.print(_worker_table(workers))
