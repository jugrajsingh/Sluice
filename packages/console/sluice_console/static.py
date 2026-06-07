from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def mount_web(app: FastAPI, directory: str) -> None:
    app.mount("/", StaticFiles(directory=directory, html=True), name="web")
