"""What every browser sign-in by redirect shares: the return target, its cookies, the way back.

GitHub (`auth_github`) and Google (`auth_google`) run the same shape: `/authorize` → the
provider → `/callback` → the app, which collects its token at `/exchange`. The harness serves no
pages, so every outcome of the callback is a redirect back to the app with a fragment
(`#felix_login=ok` or `#felix_login_error=<code>`); a fragment never reaches a server or a log.
The token itself never appears in a URL: the callback leaves it in a sealed HttpOnly cookie that
lives for one page load, and `/exchange` hands it to the page and clears it.

No route lives here; the token and refusal shapes every sign-in answers with do.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from felix.auth.github import LoginErrorCode
from pydantic import BaseModel

# From `/authorize` to the callback: how long a person may spend on the provider's screen.
FLOW_TTL_S = 600
# From the callback to the page collecting its token: one page load.
HANDOFF_TTL_S = 60


class LoginTokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    tenant: str
    scopes: list[str]
    # The GitHub user who logged in; empty from `/actions`, because a workflow is not a person.
    github_login: str = ""


class LoginErrorOut(BaseModel):
    """Every refusal. `interval` accompanies 428; `tenants` accompanies 409; `github_login`
    accompanies a refusal made after GitHub said who signed in."""

    error: LoginErrorCode
    message: str
    interval: int | None = None
    tenants: list[str] | None = None
    github_login: str | None = None


def return_target(origins: tuple[str, ...], return_to: str) -> tuple[str, str]:
    """`return_to` checked against the login's allowed origins, as (origin, the URL itself).
    Raises 400 for anything else: this is the only redirect target a caller can name."""
    from urllib.parse import urlsplit

    parts = urlsplit(return_to)
    origin = f"{parts.scheme}://{parts.netloc}"
    if not parts.scheme or not parts.netloc or origin not in origins:
        raise HTTPException(status_code=400, detail="return_to_not_allowed")
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        raise HTTPException(status_code=400, detail="return_to_not_allowed")
    # The fragment is ours to write on the way back.
    return origin, return_to.split("#", 1)[0]


def set_cookie(response: Any, name: str, value: str, *, max_age: int, secure: bool, same_site: str) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite=same_site
    )


def clear_cookie(response: Any, name: str, *, secure: bool) -> None:
    """Expire a sign-in cookie, with the same attributes it was set with."""
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def back(return_to: str, fragment: str) -> Any:
    from fastapi.responses import RedirectResponse

    return RedirectResponse(f"{return_to}#{fragment}", status_code=302)


def pkce_pair() -> tuple[str, str]:
    """A fresh PKCE (verifier, S256 challenge)."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge
