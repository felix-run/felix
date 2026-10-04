"""A fake github.com + api.github.com at the transport, for the GitHub login tests.

Shared by `tests/unit/test_auth_github.py` and `tests/e2e/test_github_login.py` so both drive
the same GitHub: the device flow, `/user`, one membership read per org, and the Actions OIDC
issuer's key set (`actions_id_token` signs tokens it verifies).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from joserfc import jwk, jwt

ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
ACTIONS_AUDIENCE = "https://felix.example.test"
ACTIONS_KEY = jwk.RSAKey.generate_key(2048, parameters={"kid": "actions-1"})
DEPLOY_REPO_ID = 9001

# Numeric org ids: the identity a mapping pins, since an org *name* can be re-registered.
ORG_IDS = {"acme": 101, "globex": 102, "acme-ops": 103}


def org(name: str, tenant: str, scopes: list[str] | None = None) -> dict[str, Any]:
    return {"id": ORG_IDS[name.lower()], "tenant": tenant, "scopes": scopes or []}


@dataclass
class FakeGitHub:
    """github.com and api.github.com, as far as the device flow and membership reads go."""

    polls: list[dict[str, Any]] = field(default_factory=lambda: [{"access_token": "gho_x"}])
    user: dict[str, Any] = field(default_factory=lambda: {"id": 4242, "login": "octo"})
    # org (as requested) -> (status, membership state)
    memberships: dict[str, tuple[int, str]] = field(default_factory=lambda: {"acme": (200, "active")})
    api_status: int = 200
    # Status for the membership reads only, after `/user` has answered 200.
    membership_status: int | None = None
    # org (lowercased) -> the id GitHub reports for it now
    org_ids: dict[str, int] = field(default_factory=lambda: dict(ORG_IDS))
    device_code_body: dict[str, Any] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    # Device codes are issued `dev-1`, `dev-2`, … and are single-use, as GitHub's are: once a
    # poll has returned a token for one, every later poll for it is `incorrect_device_code`.
    issued: int = 0
    redeemed: set[str] = field(default_factory=set)
    # The key set token.actions.githubusercontent.com publishes, and how often it was fetched.
    actions_keys: list[Any] = field(default_factory=lambda: [ACTIONS_KEY])
    actions_jwks_fetches: int = 0
    # The GitHub App half: redirect sign-in, refresh-token rotation and grant revocation.
    client_secret: str = "app-secret"
    # code -> (code_challenge it was issued for, the token answer)
    web_codes: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    # Refresh tokens GitHub still honours -> the answer a refresh returns. Spent on use.
    refresh_answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    refreshes: int = 0
    revoked_grants: list[str] = field(default_factory=list)
    # Access tokens api.github.com accepts. `gho_x` is the device flow's.
    access_tokens: set[str] = field(default_factory=lambda: {"gho_x"})

    def issue_web_code(self, code_challenge: str, answer: dict[str, Any] | None = None) -> str:
        """What github.com does when the person approves: a code bound to this PKCE challenge."""
        code = f"code-{len(self.web_codes) + 1}"
        self.web_codes[code] = (code_challenge, answer or app_grant("gho_x", "ghr_1"))
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "github.com" and path == "/login/device/code":
            if self.device_code_body is not None:
                return httpx.Response(200, json=self.device_code_body)
            self.issued += 1
            return httpx.Response(
                200,
                json={
                    "device_code": f"dev-{self.issued}",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("grant_type") == "refresh_token":
                return self._refresh(form)
            if "code" in form:
                return self._exchange_code(form)
            device_code = form["device_code"]
            if device_code in self.redeemed:
                return httpx.Response(200, json={"error": "incorrect_device_code"})
            if not self.polls:
                # Not an assert: raised inside a route it would surface as a bare 500.
                return httpx.Response(200, json={"error": "fake_github_ran_out_of_polls"})
            answer = self.polls.pop(0)
            if "access_token" in answer:
                self.redeemed.add(device_code)
            return httpx.Response(200, json=answer)
        if request.url.host == "token.actions.githubusercontent.com":
            assert path == "/.well-known/jwks", request.url
            self.actions_jwks_fetches += 1
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.actions_keys]})
        assert request.url.host == "api.github.com", request.url
        if request.method == "DELETE" and path.startswith("/applications/") and path.endswith("/grant"):
            return self._revoke_grant(request)
        assert request.headers["authorization"].removeprefix("Bearer ") in self.access_tokens, request.headers
        if self.api_status != 200:
            return httpx.Response(self.api_status, json={"message": "boom"})
        if path == "/user":
            return httpx.Response(200, json=self.user)
        if self.membership_status is not None:
            return httpx.Response(self.membership_status, json={"message": "boom"})
        org = path.removeprefix("/user/memberships/orgs/")
        status, state = self.memberships.get(org, (404, ""))
        organization = {"login": org, "id": self.org_ids.get(org.lower())}
        return httpx.Response(status, json={"state": state, "organization": organization})

    def _exchange_code(self, form: dict[str, str]) -> httpx.Response:
        import base64
        import hashlib

        if form.get("client_secret") != self.client_secret:
            return httpx.Response(200, json={"error": "incorrect_client_credentials"})
        issued = self.web_codes.pop(form["code"], None)
        if issued is None:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        challenge, answer = issued
        verifier = form.get("code_verifier", "")
        derived = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if derived != challenge:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        self._honour(answer)
        return httpx.Response(200, json=answer)

    def _refresh(self, form: dict[str, str]) -> httpx.Response:
        if form.get("client_secret") != self.client_secret:
            return httpx.Response(200, json={"error": "incorrect_client_credentials"})
        answer = self.refresh_answers.pop(form["refresh_token"], None)
        if answer is None:
            return httpx.Response(200, json={"error": "bad_refresh_token"})
        self.refreshes += 1
        self._honour(answer)
        return httpx.Response(200, json=answer)

    def _honour(self, answer: dict[str, Any]) -> None:
        if token := answer.get("access_token"):
            self.access_tokens.add(token)

    def _revoke_grant(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        token = _json.loads(request.content)["access_token"]
        self.revoked_grants.append(token)
        self.access_tokens.discard(token)
        return httpx.Response(204)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def actions_claims(**overrides: Any) -> dict[str, Any]:
    """An Actions ID token's claims for `acme/deploy` on main, as GitHub issues them."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ACTIONS_ISSUER,
        "aud": ACTIONS_AUDIENCE,
        "sub": "repo:acme/deploy:ref:refs/heads/main",
        "repository": "acme/deploy",
        "repository_id": str(DEPLOY_REPO_ID),
        # GitHub sends ids as strings.
        "repository_owner": "acme",
        "repository_owner_id": str(ORG_IDS["acme"]),
        "ref": "refs/heads/main",
        "sha": "0" * 40,
        "run_id": "777",
        "workflow_ref": "acme/deploy/.github/workflows/ship.yml@refs/heads/main",
        # The file that actually ran; differs from `workflow_ref` when a reusable workflow is called.
        "job_workflow_ref": "acme/deploy/.github/workflows/ship.yml@refs/heads/main",
        "event_name": "push",
        "run_attempt": "1",
        "actor": "octo",
        "triggering_actor": "octo",
        "jti": "jti-1",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def actions_id_token(key: Any = None, *, alg: str = "RS256", **overrides: Any) -> str:
    key = key or ACTIONS_KEY
    header = {"alg": alg, "kid": key.kid} if getattr(key, "kid", None) else {"alg": alg}
    return jwt.encode(header, actions_claims(**overrides), key)


def app_grant(access: str, refresh: str, *, expires_in: int = 28_800) -> dict[str, Any]:
    """A GitHub App's token answer with expiring user tokens: 8h access, ~6 months refresh."""
    return {
        "access_token": access,
        "expires_in": expires_in,
        "refresh_token": refresh,
        "refresh_token_expires_in": 15_897_600,
        "token_type": "bearer",
        "scope": "",
    }
