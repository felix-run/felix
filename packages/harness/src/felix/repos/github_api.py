"""GitHub, read as the person: the repositories their App authorization reaches.

A GitHub App's user token sees exactly the repositories where the App is installed *and* the
person has access, so that intersection is all this can list — it cannot count the repositories
the person could see without the App, which is why a client is given the App's install link
instead. Every call goes through `felix.auth.github.github_http_client`, the one seam the tests
replace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    import httpx

    from felix.config import Settings

# Pages walked per listing. A person in a large org can reach thousands of repositories; past
# this the listing says it is truncated and a client filters by name (`q`) instead.
MAX_PAGES = 10
PER_PAGE = 100


class GitHubReadError(Exception):
    """GitHub answered a read with something other than the data (status kept for the route)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _headers(token: str) -> dict[str, str]:
    from felix.auth.github import _API_HEADERS

    return {**_API_HEADERS, "authorization": f"Bearer {token}"}


async def _get(client: httpx.AsyncClient, path: str, token: str, params: dict[str, Any] | None = None) -> Any:
    import httpx

    from felix.auth.github import GITHUB_API_URL

    try:
        resp = await client.get(f"{GITHUB_API_URL}{path}", headers=_headers(token), params=params)
    except httpx.HTTPError as exc:
        raise GitHubReadError(502, f"GitHub unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise GitHubReadError(resp.status_code, f"GET {path} answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GitHubReadError(502, f"GET {path} answered non-JSON") from exc


def repo_view(repo: dict[str, Any], installation_id: int | None = None) -> dict[str, Any]:
    """The fields a client needs to show a repository, and whether this person can write to it."""
    raw = repo.get("permissions")
    permissions: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "full_name": str(repo.get("full_name") or ""),
        "private": bool(repo.get("private")),
        "archived": bool(repo.get("archived")),
        "default_branch": str(repo.get("default_branch") or ""),
        # The person's own permission; the App's is its configuration (Contents: write).
        "can_write": bool(permissions.get("push")) and not bool(repo.get("archived")),
        "size_kb": int(repo.get("size") or 0),
        "installation_id": installation_id,
    }


async def list_reachable(
    settings: Settings, token: str, *, q: str = "", client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Installations this person can see, and the repositories under them, filtered by `q`."""
    from felix.auth.github import client_scope

    needle = q.strip().lower()
    installations: list[dict[str, Any]] = []
    repositories: list[dict[str, Any]] = []
    truncated = False
    async with client_scope(client, settings) as http:
        body = await _get(http, "/user/installations", token, {"per_page": PER_PAGE})
        for inst in body.get("installations") or []:
            account = inst.get("account") if isinstance(inst.get("account"), dict) else {}
            installations.append(
                {
                    "id": int(inst.get("id") or 0),
                    "account": str(account.get("login") or ""),
                    "account_type": str(account.get("type") or ""),
                    "repository_selection": str(inst.get("repository_selection") or ""),
                }
            )
        for inst in installations:
            for page in range(1, MAX_PAGES + 1):
                listed = await _get(
                    http,
                    f"/user/installations/{inst['id']}/repositories",
                    token,
                    {"per_page": PER_PAGE, "page": page},
                )
                batch = listed.get("repositories") or []
                for repo in batch:
                    view = repo_view(repo, inst["id"])
                    if not needle or needle in view["full_name"].lower():
                        repositories.append(view)
                if len(batch) < PER_PAGE:
                    break
            else:
                truncated = True
    repositories.sort(key=lambda r: r["full_name"].lower())
    slug = settings.github_app_slug.strip()
    return {
        "installations": installations,
        "repositories": repositories,
        "truncated": truncated,
        "install_url": f"https://github.com/apps/{slug}/installations/new" if slug else None,
    }


async def get_repo(
    settings: Settings, token: str, full_name: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """GitHub's repository object as this person sees it through the App, or None when the App
    cannot reach it for them (not installed there, or the person has no access)."""
    from felix.auth.github import client_scope

    owner, _, name = full_name.partition("/")
    if not owner or not name or "/" in name:
        return None
    async with client_scope(client, settings) as http:
        try:
            body = await _get(http, f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}", token)
        except GitHubReadError as exc:
            if exc.status in {403, 404}:
                return None
            raise
    return body if isinstance(body, dict) else None
