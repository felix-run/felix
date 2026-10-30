"""What the plugin's connect routes share: the connection they answer with, and how they refuse.

A connection is shown as the vault's own routes show one -- who, which account, whether it works
-- and never with its secret. A refusal is a written code and message, never an exception's text
or anything the person sent: what they sent is a credential.
"""

from __future__ import annotations

from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ConnectionView(BaseModel):
    """Who and whether it works; never the secret. Core's `ConnectionOut` less what a secret
    connection never has (scopes, an expiry): a plugin cannot import the API's models."""

    provider: str
    account: str
    label: str
    kind: Literal["oauth", "secret"]
    status: Literal["active", "revoked"]
    created_at: int
    updated_at: int


def refused(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


__all__ = ["ConnectionView", "refused"]
