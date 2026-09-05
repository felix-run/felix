"""Granian / uvicorn entry for the Felix API."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from felix.config import Settings


def create_application():
    """ASGI factory used by granian/uvicorn ``felix_api.main:create_application``."""
    from felix_api.app import create_app

    return create_app()


def main() -> None:
    """CLI entry: prefer Granian, fall back to uvicorn."""
    from felix.config import get_settings

    settings = get_settings()
    host = settings.host
    port = settings.port
    workers = settings.workers

    try:
        from granian.constants import Interfaces
        from granian.server import Server

        Server(
            "felix_api.main:create_application",
            address=host,
            port=port,
            interface=Interfaces.ASGI,
            workers=workers,
            factory=True,
            **server_options(settings),
        ).serve()
    except ImportError:
        import uvicorn

        uvicorn.run(
            "felix_api.main:create_application",
            host=host,
            port=port,
            factory=True,
            workers=workers,
            backlog=settings.http_backlog,
            timeout_graceful_shutdown=settings.graceful_shutdown_seconds,
        )


class GranianOptions(TypedDict):
    backlog: int
    runtime_threads: int
    workers_kill_timeout: int
    respawn_failed_workers: bool


def server_options(settings: Settings) -> GranianOptions:
    """Granian options beyond host/port/workers, all from `Settings`.

    On SIGTERM Granian joins each worker for `graceful_shutdown_seconds` and then kills
    it — a deadline on the drain, not the drain itself, which is why the default matches
    the chart's grace period rather than cutting under it.
    """
    return {
        "backlog": settings.http_backlog,
        "runtime_threads": settings.http_runtime_threads,
        "workers_kill_timeout": settings.graceful_shutdown_seconds,
        "respawn_failed_workers": settings.respawn_failed_workers,
    }
