"""API keys holding one scope each, so a positive case proves that scope and not the admin bypass."""

from __future__ import annotations

import json

ADMIN = "sk-admin-not-a-secret"
READER = "sk-reader-not-a-secret"
WRITER = "sk-writer-not-a-secret"


def scoped_keys(*, reader: list[str], writer: list[str] | None = None) -> dict[str, str]:
    """Three keys: `admin` bypasses everything, `reader` and `writer` hold one scope each.

    The precise scopes matter. `admin` satisfies every gate by design, so a positive case
    driven with the admin key cannot tell "this scope grants access" from "admin bypasses the
    check" — and a route whose `require_mgmt_scopes` call was deleted would still pass it. The
    writer key holds only the write scope under test, so its success is evidence about that
    scope and nothing else.
    """
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ADMIN: {"tenant_id": "default", "sub": "admin", "scopes": ["admin"]},
                READER: {"tenant_id": "default", "sub": "reader", "scopes": reader},
                WRITER: {"tenant_id": "default", "sub": "writer", "scopes": writer or []},
            }
        ),
    }


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
