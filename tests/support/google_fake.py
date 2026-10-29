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

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

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


# Google answers `email` as this URL in a grant's `scope`.
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


def _granted(scope: str) -> set[str]:
    return {USERINFO_EMAIL if s == "email" else s for s in scope.split()}


class FakeGoogle:
    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_calls = 0
        self.jwks_calls = 0
        # Offline grants (connecting), as Google keeps them: one per Google account (`sub`) and this
        # client, holding the union of every scope ever granted, and the refresh tokens issued.
        self.grants: dict[str, set[str]] = {}
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> sub
        self.access_tokens: list[str] = []
        self.refreshes = 0
        self.revoked: list[str] = []
        # Failures on demand: (status, body) the next call of that kind answers with.
        self.fail_token: tuple[int, Any] | None = None
        self.fail_refresh: tuple[int, Any] | None = None
        self.fail_revoke: tuple[int, Any] | None = None

    def issue_code(
        self, authorize_query: dict[str, str], *, grant_only: str | None = None, **claim_overrides: Any
    ) -> str:
        """The person approving. `grant_only` is the scope they left ticked (granular consent)."""
        # Checked when present: some unit tests hand over only the fields their case is about.
        assert authorize_query.get("client_id", CLIENT_ID) == CLIENT_ID, "authorize URL for another client"
        assert authorize_query.get("response_type", "code") == "code"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": authorize_query["code_challenge"],
            # Google's default is `plain`; a hand-built query (no client_id) means the harness's S256.
            "method": authorize_query.get(
                "code_challenge_method", "plain" if "client_id" in authorize_query else "S256"
            ),
            "redirect_uri": authorize_query["redirect_uri"],
            "claims": google_claims(nonce=authorize_query["nonce"], **claim_overrides),
            "offline": authorize_query.get("access_type") == "offline",
            "consent": "consent" in authorize_query.get("prompt", "").split(),
            "incremental": authorize_query.get("include_granted_scopes") == "true",
            "scope": grant_only if grant_only is not None else authorize_query.get("scope", "openid email"),
        }
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == GOOGLE_JWKS_URL:
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.method == "POST" and url == GOOGLE_TOKEN_URL:
            self.token_calls += 1
            if form.get("grant_type") == "refresh_token":
                return self._refresh(form)
            return self._token(form)
        if request.method == "POST" and url == GOOGLE_REVOKE_URL:
            return self._revoke(form)
        return httpx.Response(404, json={"error": "not_found"})

    @staticmethod
    def _failed(failure: tuple[int, Any]) -> httpx.Response:
        status, body = failure
        return (
            httpx.Response(status, json=body)
            if not isinstance(body, str)
            else httpx.Response(status, text=body)
        )

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if self.fail_token is not None:
            return self._failed(self.fail_token)
        if form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None or form.get("redirect_uri") != issued["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        if issued["method"] == "S256":
            digest = hashlib.sha256(verifier.encode()).digest()
            verifier = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        if verifier != issued["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        body: dict[str, Any] = {
            "access_token": "unused-access-token",
            "expires_in": 3599,
            "token_type": "Bearer",
            "scope": issued["scope"],
            "id_token": id_token(issued["claims"]),
        }
        if issued["offline"]:
            body |= self._offline(issued)
        return httpx.Response(200, json=body)

    def _offline(self, issued: dict[str, Any]) -> dict[str, Any]:
        sub = issued["claims"]["sub"]
        already = sub in self.grants
        held = self.grants.setdefault(sub, set())
        held |= _granted(issued["scope"])
        access = f"ya29.{secrets.token_urlsafe(8)}"
        self.access_tokens.append(access)
        out: dict[str, Any] = {
            "access_token": access,
            "scope": " ".join(sorted(held if issued["incremental"] else _granted(issued["scope"]))),
        }
        # Google sends a refresh token on a consent screen, or for an account's first grant.
        if issued["consent"] or not already:
            refresh = f"1//{secrets.token_urlsafe(12)}"
            self.refresh_tokens[refresh] = sub
            out["refresh_token"] = refresh
        return out

    def _refresh(self, form: dict[str, str]) -> httpx.Response:
        """Google keeps the refresh token on a refresh and refuses a revoked one."""
        if self.fail_refresh is not None:
            return self._failed(self.fail_refresh)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        sub = self.refresh_tokens.get(form.get("refresh_token", ""))
        if sub is None or sub not in self.grants:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refreshes += 1
        scope = " ".join(sorted(self.grants[sub]))
        return httpx.Response(
            200, json={"access_token": f"ya29.refreshed-{self.refreshes}", "expires_in": 3599, "scope": scope}
        )

    def _revoke(self, form: dict[str, str]) -> httpx.Response:
        """Revoking any token of a grant ends the whole grant: every refresh token of that account."""
        if self.fail_revoke is not None:
            return self._failed(self.fail_revoke)
        token = form.get("token", "")
        self.revoked.append(token)
        sub = self.refresh_tokens.get(token)
        if sub is not None:
            self.grants.pop(sub, None)
            for refresh in [r for r, s in self.refresh_tokens.items() if s == sub]:
                del self.refresh_tokens[refresh]
        return httpx.Response(200, json={})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
