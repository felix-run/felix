"""Felix auth — JWT, API key, and anonymous modes."""

from __future__ import annotations

from felix.auth.context import (
    ANONYMOUS,
    BUILTIN_AUTH_MODES,
    AuthContext,
    Principal,
    require_scope,
    require_scopes,
)
from felix.auth.jwt import VerifierConfig, parse_verifiers, verify_jwt
from felix.auth.mgmt import require_mgmt_scopes
from felix.auth.middleware import AuthMiddleware, authenticate_request, require_authenticated

__all__ = [
    "ANONYMOUS",
    "BUILTIN_AUTH_MODES",
    "AuthContext",
    "AuthMiddleware",
    "Principal",
    "VerifierConfig",
    "authenticate_request",
    "parse_verifiers",
    "require_authenticated",
    "require_mgmt_scopes",
    "require_scope",
    "require_scopes",
    "verify_jwt",
]
