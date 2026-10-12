"""A fake Google: the OAuth token endpoint and the JWKS, over `httpx.MockTransport`.

`issue_code` plays the person approving on Google's screen: given the authorize query the harness
sent the browser with, it returns a code bound to that query's PKCE challenge, redirect URI and
nonce. The token endpoint then checks the client secret, the redirect URI and the PKCE verifier
the way Google does, and answers with an ID token signed by `KEY` — whose claims a test can
override per code.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
from felix.auth.google import GOOGLE_JWKS_URL, GOOGLE_TOKEN_URL
from joserfc import jwk, jwt

CLIENT_ID = "123-e2e.apps.googleusercontent.com"
CLIENT_SECRET = "google-e2e-secret"
KID_SUB = "1000000000000000001"
KEY = jwk.RSAKey.generate_key(2048, parameters={"kid": "google-test-1"})


def id_token(claims: dict[str, Any], key: Any = KEY) -> str:
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)


def google_claims(*, nonce: str, **overrides: Any) -> dict[str, Any]:
    """An ID token's claims for the child's account, as Google issues them."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": KID_SUB,
        "email": "kid@example.com",
        "email_verified": True,
        "nonce": nonce,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class FakeGoogle:
    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_calls = 0
        self.jwks_calls = 0

    def issue_code(self, authorize_query: dict[str, str], **claim_overrides: Any) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": authorize_query["code_challenge"],
            "redirect_uri": authorize_query["redirect_uri"],
            "claims": google_claims(nonce=authorize_query["nonce"], **claim_overrides),
        }
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == GOOGLE_JWKS_URL:
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if request.method == "POST" and url == GOOGLE_TOKEN_URL:
            self.token_calls += 1
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return self._token(form)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None or form.get("redirect_uri") != issued["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip("=") != issued["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": "unused-access-token",
                "expires_in": 3599,
                "token_type": "Bearer",
                "scope": "openid email",
                "id_token": id_token(issued["claims"]),
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
