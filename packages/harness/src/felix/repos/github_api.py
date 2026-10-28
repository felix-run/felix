"""GitHub, read as the person: the repositories their App authorization reaches, and the issues
and pull requests in one of them.

A GitHub App's user token sees exactly the repositories where the App is installed *and* the
person has access, so that intersection is all this can list — it cannot count the repositories
the person could see without the App, which is why a client is given the App's install link
instead. Every call goes through `felix.auth.github.github_http_client`, the one seam the tests
replace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
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

    segments = _segments(full_name)
    if segments is None:
        return None
    owner, name = segments
    async with client_scope(client, settings) as http:
        try:
            body = await _get(http, f"/repos/{owner}/{name}", token)
        except GitHubReadError as exc:
            if exc.status in {403, 404}:
                return None
            raise
    return body if isinstance(body, dict) else None


# Issues and pull requests, for starting a conversation from one. GitHub's issues listing carries
# pull requests too (each with a `pull_request` key), so one walk serves both kinds and the kind is
# filtered here, as `q` is — the same client-side filtering `list_reachable` does, over a bounded
# walk, rather than GitHub's search API, which has a far smaller rate limit of its own.
MAX_ITEMS = 30
MAX_ITEM_PAGES = 5
# An issue's body is whatever its author wrote, up to GitHub's 65,536 characters. A client puts it
# in a composer, where a body past this is not read; the cut is marked by `body_truncated`.
MAX_BODY_CHARS = 20_000

ItemKind = Literal["issue", "pull"]
ItemState = Literal["open", "closed", "all"]
# Whether the issues listing could be read: `disabled` is a repository with issues turned off
# (GitHub's 410), `forbidden` a token without the App's Issues permission (403) — GitHub documents
# the issues endpoints under Issues alone, so a Pull-requests-only App is refused them, pull
# requests included. Either way pull requests come from the pulls API instead, and a client can
# say "pull requests only" rather than "no open issues".
IssueAccess = Literal["ok", "disabled", "forbidden"]
# What the issues endpoints answer when the pulls API can stand in.
_ISSUES_UNREADABLE: dict[int, IssueAccess] = {410: "disabled", 403: "forbidden"}


def _segments(full_name: str) -> tuple[str, str] | None:
    """`owner/name` as two URL-safe path segments, or None for anything that is not that shape.

    A segment of dots alone is refused: quoted, it is still `..`, which a URL resolves upward and
    would turn `/repos/../user` into a read of a different endpoint with the person's token.
    """
    owner, _, name = full_name.partition("/")
    if not owner or not name or "/" in name or {owner, name} & {".", ".."}:
        return None
    return quote(owner, safe=""), quote(name, safe="")


def _html_url(full_name: str, kind: str, number: int) -> str:
    """The item's page, built from values already checked rather than taken from the response."""
    from felix.auth.github import GITHUB_URL

    return f"{GITHUB_URL}/{full_name}/{'pull' if kind == 'pull' else 'issues'}/{number}"


def item_view(raw: dict[str, Any], full_name: str, *, kind: ItemKind | None = None) -> dict[str, Any]:
    """The fields a client shows for an issue or pull request. `kind` overrides the shape check,
    for an object from the pulls API, which carries no `pull_request` key of its own."""
    resolved: ItemKind = kind or ("pull" if isinstance(raw.get("pull_request"), dict) else "issue")
    user = raw.get("user")
    number = int(raw.get("number") or 0)
    return {
        "number": number,
        "kind": resolved,
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or ""),
        "html_url": _html_url(full_name, resolved, number),
        # A deleted account is `null` on GitHub's side.
        "author": str(user.get("login") or "") if isinstance(user, dict) else "",
        "updated_at": str(raw.get("updated_at") or ""),
    }


async def list_items(
    settings: Settings,
    token: str,
    full_name: str,
    *,
    kind: ItemKind | Literal["all"] = "all",
    state: ItemState = "open",
    q: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """The repository's issues and pull requests as this person sees them, most recently updated
    first, at most `MAX_ITEMS`, filtered by kind and by `q` against the title.

    `full_name` is the repository `get_repo` answered with, so reachability is already settled.
    When the issues listing cannot be read — issues turned off (410), or a token without the
    Issues permission (403) — pull requests are read from the pulls listing instead, and
    `issue_access` says which. A 403 the pulls listing also answers (a rate limit, say) is raised
    from there.
    """
    from felix.auth.github import client_scope

    segments = _segments(full_name)
    if segments is None:
        raise GitHubReadError(400, "not an owner/name repository")
    owner, name = segments
    needle = q.strip().lower()
    items: list[dict[str, Any]] = []
    truncated = False
    params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc", "per_page": PER_PAGE}
    path, pulls_only = f"/repos/{owner}/{name}/issues", False
    access: IssueAccess = "ok"
    async with client_scope(client, settings) as http:
        page = 1
        while True:
            try:
                batch = await _get(http, path, token, {**params, "page": page})
            except GitHubReadError as exc:
                if exc.status not in _ISSUES_UNREADABLE or pulls_only or page != 1:
                    raise
                access = _ISSUES_UNREADABLE[exc.status]
                if kind == "issue":
                    break
                path, pulls_only = f"/repos/{owner}/{name}/pulls", True
                continue
            if not isinstance(batch, list):
                raise GitHubReadError(502, f"GET {path} answered something other than a list")
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                view = item_view(raw, full_name, kind="pull" if pulls_only else None)
                if kind != "all" and view["kind"] != kind:
                    continue
                if needle and needle not in view["title"].lower():
                    continue
                if len(items) == MAX_ITEMS:
                    truncated = True
                    break
                items.append(view)
            if truncated or len(batch) < PER_PAGE:
                break
            if page == MAX_ITEM_PAGES:
                # More on GitHub than one listing walks: narrow with `q`.
                truncated = True
                break
            page += 1
    return {"repository": full_name, "items": items, "truncated": truncated, "issue_access": access}


async def get_item(
    settings: Settings, token: str, full_name: str, number: int, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """One issue or pull request with its body, or None when the repository has no such number.

    The body is the author's markdown, returned as text and nothing else: it is third-party input,
    and whatever reads it decides what it is.
    """
    from felix.auth.github import client_scope

    segments = _segments(full_name)
    if segments is None:
        return None
    owner, name = segments
    async with client_scope(client, settings) as http:
        try:
            raw = await _get(http, f"/repos/{owner}/{name}/issues/{int(number)}", token)
            kind: ItemKind | None = None
        except GitHubReadError as exc:
            if exc.status == 404:
                return None
            if exc.status not in _ISSUES_UNREADABLE:
                raise
            # Issues turned off, or not readable with this token: the number can still be a pull
            # request. One that is an issue then reads as not found, which is all this token can see.
            try:
                raw = await _get(http, f"/repos/{owner}/{name}/pulls/{int(number)}", token)
            except GitHubReadError as inner:
                if inner.status == 404:
                    return None
                raise
            kind = "pull"
    if not isinstance(raw, dict):
        raise GitHubReadError(502, "an issue read answered something other than an object")
    body = raw.get("body")
    text = body if isinstance(body, str) else ""
    return {
        **item_view(raw, full_name, kind=kind),
        "body": text[:MAX_BODY_CHARS],
        "body_truncated": len(text) > MAX_BODY_CHARS,
    }
