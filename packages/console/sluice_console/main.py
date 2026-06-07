from __future__ import annotations

import os


def create_app():
    """Compose the console for `uvicorn sluice_console.main:app`.

    Imports the k8s-backed inspector lazily so importing this module in tests
    (or building docs) never requires a cluster.
    """
    from sluice_autoscaler.k8s import KubeClusterInspector  # noqa: PLC0415
    from sluice_core.config import Settings
    from sluice_drivers.factory import build_queue, build_registry

    from .app import build_console_app
    from .static import mount_web

    settings = Settings()
    inspector = KubeClusterInspector(
        in_cluster=os.getenv("CONSOLE__IN_CLUSTER", "1") == "1",
        config_path=os.getenv("KUBECONFIG"),
        namespace=os.getenv("CONSOLE__WORKERS_NAMESPACE", "default"),
    )
    application = build_console_app(registry=build_registry(settings), queue=build_queue(settings), inspector=inspector)
    mount_web(application, os.getenv("CONSOLE_WEB_DIR", "web"))

    @application.on_event("startup")
    async def _open() -> None:
        await inspector.open()

    return application


app = create_app() if os.getenv("PYTEST_CURRENT_TEST") is None and os.getenv("CONSOLE_EAGER", "1") == "1" else None
