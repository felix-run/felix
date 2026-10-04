"""Per-person repositories: what you can reach through the GitHub App, and a thread's checkout.

- `GET /github/repos?q=` lists the repositories your GitHub App authorization reaches — where the
  App is installed and you have access — with the App's install link for the rest.
- `POST /chat/sessions/{thread_id}/workspace/repo` clones one of them into the thread's own
  checkout, where the agent's workspace and shell tools then work (`felix.repos.checkouts`).
- `GET` and `DELETE` on the same path report and remove it.

Every GitHub read and the clone act as the caller, with an access token minted from their stored
connection (`felix.auth.github_connections`). No route returns a GitHub token.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("felix_api.repos")

github_router = APIRouter(tags=["Repos"])
checkout_router = APIRouter(tags=["Repos"])


class InstallationOut(BaseModel):
    id: int
    account: str
    account_type: str
    repository_selection: str


class RepoOut(BaseModel):
    full_name: str
    private: bool
    archived: bool
    default_branch: str
    # Whether you can push to it (and it is not archived). The App's own access is its config.
    can_write: bool
    size_kb: int
    installation_id: int | None = None


class ReposOut(BaseModel):
    installations: list[InstallationOut]
    repositories: list[RepoOut]
    # More repositories than one listing walks: filter with `q`.
    truncated: bool
    # Where to install the App on more repositories; null when FELIX_GITHUB_APP_SLUG is unset.
    install_url: str | None = None


class OpenRepoRequest(BaseModel):
    model_config = {"extra": "forbid"}

    full_name: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    # The branch to check out; the repository's default branch when omitted.
    ref: str | None = Field(default=None, min_length=1, max_length=255)


class CheckoutOut(BaseModel):
    state: Literal["cloning", "ready", "failed", "expired"]
    repo: str
    base: str
    private: bool = False
    opened_by: str = ""
    created_at: int | None = None
    error: str | None = None
    branch: str | None = None
    ahead: int | None = None
    dirty: bool | None = None


class RepoErrorOut(BaseModel):
    error: str
    message: str


# What an error means to the caller, written here rather than taken from the exception. The
# callers of these routes are people, and an exception's text can carry what is the operator's
# business only: an egress proxy's name, a DNS failure, a filesystem path. Its detail goes to the
# log; the answer says whose problem it is and what to do.
GITHUB_UNAVAILABLE = "GitHub could not be reached or answered unexpectedly; try again shortly"
CHECKOUTS_MISCONFIGURED = "repository checkouts are misconfigured on this server; the cause is in its log"
CHECKOUT_BUSY = "a checkout of this thread is being made; try again once it finishes"


def _checkout_refusal_message(code: str, settings: Any, full_name: str = "") -> str:
    """A CheckoutRefused's answer, from its code and values the route already validated."""
    if code == "repository_too_large":
        return f"{full_name} is larger than the {settings.repo_clone_max_mb} MB this server checks out"
    if code == "thread_has_repository":
        return "this thread already has a repository; remove it before opening another"
    if code == "checkout_busy":
        return CHECKOUT_BUSY
    return "GitHub returned a repository this server cannot check out"


def _refusal(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _caller_token(request: Request) -> tuple[str, int, str, str] | JSONResponse:
    """(tenant, GitHub user id, subject, access token) for the caller, or the refusal to return."""
    from felix.auth import github_connections
    from felix.auth.github import GitHubLoginError

    from felix_api.routes.auth_github import _github_user_id

    tenant, user_id, subject = _github_user_id(request)
    try:
        token = await github_connections.access_token(request.app.state.settings, tenant, user_id)
    except github_connections.GitHubNotConnected:
        return _refusal(409, "github_not_connected", "sign in with GitHub to connect your account first")
    except github_connections.GitHubConnectionRevoked:
        return _refusal(
            409, "github_connection_revoked", "your GitHub connection stopped working; reconnect GitHub"
        )
    except GitHubLoginError as exc:
        logger.warning("github access token for github:%s failed: %s", user_id, exc.code)
        return _refusal(502, "github_unavailable", GITHUB_UNAVAILABLE)
    return tenant, user_id, subject, token


def _thread(request: Request, thread_id: str) -> tuple[str, str]:
    from felix.auth.mgmt import auth_from_request
    from felix.thread_ids import effective_thread_id

    tenant = auth_from_request(request).principal.tenant_id
    scoped = effective_thread_id(tenant, thread_id)
    if scoped is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return tenant, scoped


_REFUSALS: dict[int | str, dict[str, Any]] = {409: {"model": RepoErrorOut}, 502: {"model": RepoErrorOut}}


@github_router.get("/repos", response_model=ReposOut, responses=_REFUSALS)
async def list_my_repos(request: Request, q: str = "") -> Any:
    """The repositories you can open: where the App is installed and you have access."""
    from felix.repos import github_api

    got = await _caller_token(request)
    if isinstance(got, JSONResponse):
        return got
    _, _, _, token = got
    try:
        listed = await github_api.list_reachable(request.app.state.settings, token, q=q[:100])
    except github_api.GitHubReadError as exc:
        logger.warning("listing repositories failed: %s", exc)
        return _refusal(502, "github_unavailable", GITHUB_UNAVAILABLE)
    return ReposOut.model_validate(listed)


@checkout_router.post(
    "/{thread_id}/workspace/repo",
    status_code=202,
    response_model=CheckoutOut,
    responses={**_REFUSALS, 404: {"model": RepoErrorOut}, 413: {"model": RepoErrorOut}},
)
async def open_thread_repo(thread_id: str, body: OpenRepoRequest, request: Request) -> Any:
    """Clone a repository you can reach into this thread's checkout. Answers at once with
    `cloning`; poll `GET` on the same path until it is `ready` (or `failed`)."""
    from felix.audit import store as audit_store
    from felix.repos import checkouts, github_api

    settings = request.app.state.settings
    tenant, scoped = _thread(request, thread_id)
    got = await _caller_token(request)
    if isinstance(got, JSONResponse):
        return got
    _, user_id, subject, token = got
    try:
        repo = await github_api.get_repo(settings, token, body.full_name)
    except github_api.GitHubReadError as exc:
        logger.warning("reading %s failed: %s", body.full_name, exc)
        return _refusal(502, "github_unavailable", GITHUB_UNAVAILABLE)
    if repo is None:
        return _refusal(
            404,
            "repository_unreachable",
            f"the GitHub App cannot reach {body.full_name} for you: install it there, or ask for access",
        )
    try:
        state = await checkouts.open_checkout(
            settings,
            tenant,
            scoped,
            repo=repo,
            github_user_id=user_id,
            opened_by=subject,
            token=token,
            base=body.ref,
        )
    except checkouts.CheckoutRefused as exc:
        status = 413 if exc.code == "repository_too_large" else 409
        return _refusal(status, exc.code, _checkout_refusal_message(exc.code, settings, body.full_name))
    except ValueError as exc:  # a checkout root nested in the shared workspace
        logger.error("repository checkouts are misconfigured: %s", exc)
        return _refusal(409, "checkouts_misconfigured", CHECKOUTS_MISCONFIGURED)
    audit_store.record_event(
        settings,
        tenant,
        "thread_repo_opened",
        principal_subj=subject,
        status=state["state"],
        payload={"thread_id": thread_id, "repo": state["repo"], "base": state["base"]},
    )
    return CheckoutOut.model_validate(state)


@checkout_router.get(
    "/{thread_id}/workspace/repo", response_model=CheckoutOut, responses={404: {"model": RepoErrorOut}}
)
async def get_thread_repo(thread_id: str, request: Request) -> Any:
    """This thread's checkout: its state, and once ready, its branch, commits ahead and whether
    it has uncommitted changes."""
    from felix.repos import checkouts

    tenant, scoped = _thread(request, thread_id)
    described = await checkouts.describe(request.app.state.settings, tenant, scoped)
    if described is None:
        return _refusal(404, "no_repository", "this thread has no repository")
    return CheckoutOut.model_validate(described)


@checkout_router.delete(
    "/{thread_id}/workspace/repo",
    status_code=204,
    response_model=None,
    responses={404: {"model": RepoErrorOut}, 409: {"model": RepoErrorOut}},
)
async def remove_thread_repo(thread_id: str, request: Request) -> Any:
    """Delete this thread's checkout. Commits not yet published are lost with it."""
    from felix.audit import store as audit_store
    from felix.auth.mgmt import auth_from_request
    from felix.repos import checkouts

    settings = request.app.state.settings
    tenant, scoped = _thread(request, thread_id)
    try:
        removed = checkouts.remove_checkout(settings, tenant, scoped)
    except checkouts.CheckoutRefused as exc:
        return _refusal(409, exc.code, _checkout_refusal_message(exc.code, settings))
    if not removed:
        return _refusal(404, "no_repository", "this thread has no repository")
    audit_store.record_event(
        settings,
        tenant,
        "thread_repo_removed",
        principal_subj=auth_from_request(request).principal.subject,
        status="removed",
        payload={"thread_id": thread_id},
    )
    from fastapi import Response

    return Response(status_code=204)
