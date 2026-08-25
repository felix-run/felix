"""Granian / uvicorn entry for the Felix API."""

from __future__ import annotations


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
        ).serve()
    except ImportError:
        import uvicorn

        uvicorn.run(
            "felix_api.main:create_application",
            host=host,
            port=port,
            factory=True,
            workers=workers,
        )


if __name__ == "__main__":
    main()
