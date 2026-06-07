from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from sluice_core.app_yaml import parse_app_yaml


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sluice", description="Sluice control CLI")
    p.add_argument("--api", default=os.getenv("SLUICE_API", "http://localhost:8080"))
    sub = p.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("apply", help="apply an App spec YAML")
    ap.add_argument("-f", "--file", required=True)
    ap.add_argument("--direct", action="store_true", help="write the spec store directly (bootstrap)")
    sub.add_parser("get", help="list apps")
    for verb in ("status", "delete", "pause", "resume"):
        sp = sub.add_parser(verb)
        sp.add_argument("name")
    return p


async def _direct_put(text: str) -> None:
    from sluice_core.config import Settings
    from sluice_drivers.factory import build_registry

    await build_registry(Settings()).put_app(parse_app_yaml(text))


def run(argv: list[str], *, client: httpx.Client | None = None) -> int:
    args = _parser().parse_args(argv)
    c = client or httpx.Client(base_url=args.api, timeout=30)
    if args.cmd == "apply":
        text = Path(args.file).read_text()
        try:
            spec = parse_app_yaml(text)
        except ValueError as e:
            print(f"invalid spec: {e}", file=sys.stderr)
            return 2
        if args.direct:
            asyncio.run(_direct_put(text))
            print(f"applied {spec.name} (direct)")
            return 0
        r = c.put(f"/v1/apps/{spec.name}", content=text, headers={"content-type": "application/yaml"})
        print(r.text)
        return 0 if r.is_success else 1
    if args.cmd == "get":
        r = c.get("/v1/apps")
        print(json.dumps(r.json(), indent=2))
        return 0 if r.is_success else 1
    if args.cmd == "status":
        r = c.get(f"/v1/apps/{args.name}")
        print(json.dumps(r.json(), indent=2))
        return 0 if r.is_success else 1
    if args.cmd == "delete":
        r = c.delete(f"/v1/apps/{args.name}")
    else:  # pause | resume
        r = c.post(f"/v1/apps/{args.name}/{args.cmd}")
    print(r.text)
    return 0 if r.is_success else 1


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))
