"""Route every `httpx.AsyncClient` the code under test builds through one transport."""

from __future__ import annotations

from typing import Any

import httpx


def bind_transport(transport: httpx.MockTransport) -> type[httpx.AsyncClient]:
    real = httpx.AsyncClient

    class _Bound(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    return _Bound
