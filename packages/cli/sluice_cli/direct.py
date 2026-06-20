from __future__ import annotations

import asyncio

from .client import CliError


def write(text: str) -> None:
    """Write an App spec straight to the registry (bootstrap), bypassing the admin API.

    Needs the optional drivers, installed via `pip install 'sluice-cli[direct]'`. The heavy
    imports live here (not at module top) so the base CLI stays thin and `--direct` is the only
    path that pulls in `sluice-drivers`.
    """
    try:
        from sluice_core.config import Settings
        from sluice_drivers.factory import build_registry
    except ImportError as e:
        raise CliError("--direct needs the drivers — install with: pip install 'sluice-cli[direct]'") from e
    from sluice_core.app_yaml import parse_app_yaml

    asyncio.run(build_registry(Settings()).put_app(parse_app_yaml(text)))
