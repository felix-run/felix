"""A fake github.com + api.github.com at the transport, for the GitHub login tests.

Shared by `tests/unit/test_auth_github.py` and `tests/e2e/test_github_login.py` so both drive
the same GitHub: the device flow, `/user`, and one membership read per org.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "github.com" and path == "/login/device/code":
            if self.device_code_body is not None:
                return httpx.Response(200, json=self.device_code_body)
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            return httpx.Response(200, json=self.polls.pop(0))
        assert request.url.host == "api.github.com", request.url
        assert request.headers["authorization"] == "Bearer gho_x"
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

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
