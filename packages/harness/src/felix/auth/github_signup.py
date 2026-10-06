"""GitHub signup: a person outside every mapped org gets a tenant of their own.

The org map (`felix.auth.github`) admits members of an operator's orgs into that org's tenant.
Signup is the other way in: a GitHub account that belongs to none of them lands in a *personal*
tenant, `gh-<numeric GitHub id>`, created by signing in. The id and never the login, because a
login can be renamed and then re-registered by someone else, and a tenant must not follow it.

`FELIX_GITHUB_SIGNUP` says who may sign up:

- `off` (the default): nobody. A non-member is `not_a_member`, exactly as before signup existed.
- `invite`: only the accounts in `FELIX_GITHUB_SIGNUP_LOGINS`; anyone else is `not_invited`.

There is deliberately no `open` yet. Every personal tenant spends the deployment's model keys,
and nothing caps one tenant's total spend, so admitting any GitHub account is a decision that
waits for that cap rather than a value someone can set today.

An org member never gets a personal tenant: the org grant wins, so signing up cannot put a
tenant chooser in front of someone who had one tenant yesterday.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.auth.github import GitHubUser, OrgGrant
    from felix.config import Settings

logger = logging.getLogger("felix.auth.github")

PERSONAL_TENANT_PREFIX = "gh-"
# A positive GitHub user id, written the one way `str(int)` writes it: `gh-007` would otherwise
# be a second spelling of `gh-7`, and two spellings of one tenant are two tenants.
_PERSONAL_TENANT_RE = re.compile(r"\Agh-([1-9][0-9]{0,19})\Z")
# GitHub's username grammar: alphanumerics and single hyphens, no leading hyphen, at most 39.
_LOGIN_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z")
# What a stranger must never hold, whatever the operator wrote.
_REFUSED_SCOPES = frozenset({"admin", "*"})


@dataclass(frozen=True, slots=True)
class Invite:
    """One entry of FELIX_GITHUB_SIGNUP_LOGINS. With `user_id`, the id is the identity and the
    login only a label; without it, the login is matched case-insensitively."""

    login: str  # lowercased
    user_id: int | None = None


def signup_enabled(settings: Settings) -> bool:
    return settings.github_signup != "off"


def personal_tenant(user_id: int) -> str:
    return f"{PERSONAL_TENANT_PREFIX}{user_id}"


def personal_tenant_owner(tenant: str) -> int | None:
    """The GitHub user id a personal tenant belongs to, or None when `tenant` is not one."""
    found = _PERSONAL_TENANT_RE.match(tenant)
    return int(found.group(1)) if found else None


@lru_cache(maxsize=4)
def parse_signup_logins(raw: str) -> tuple[Invite, ...]:
    """FELIX_GITHUB_SIGNUP_LOGINS: comma-separated `login` or `login:<id>`. Raises ValueError."""
    invites: list[Invite] = []
    seen: set[str] = set()
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        login, _, id_text = entry.partition(":")
        if not _LOGIN_RE.match(login):
            raise ValueError(f"{login!r} is not a GitHub login")
        user_id: int | None = None
        if id_text:
            if not id_text.isdigit() or int(id_text) <= 0:
                raise ValueError(
                    f"{entry!r}: the part after ':' must be the account's numeric GitHub id "
                    f"(gh api users/{login} --jq .id)"
                )
            user_id = int(id_text)
        if login.lower() in seen:
            raise ValueError(f"{login!r} is listed twice (logins are case-insensitive)")
        seen.add(login.lower())
        invites.append(Invite(login=login.lower(), user_id=user_id))
    return tuple(invites)


def signup_scopes(settings: Settings) -> tuple[str, ...]:
    from felix.auth.github import scope_list

    raw = [s.strip() for s in settings.github_signup_scopes.split(",") if s.strip()]
    return scope_list(raw, "FELIX_GITHUB_SIGNUP_SCOPES")


def is_invited(settings: Settings, user: GitHubUser) -> bool:
    for invite in parse_signup_logins(settings.github_signup_logins):
        if invite.user_id is not None:
            # Pinned: the id decides, so a renamed account keeps its invite and an account that
            # took a released login does not inherit one.
            if invite.user_id == user.id:
                return True
            if invite.login == user.login.lower():
                logger.warning(
                    "github signup: %s (github:%s) holds the login invited as github:%s; not admitted",
                    user.login,
                    user.id,
                    invite.user_id,
                )
            continue
        if invite.login == user.login.lower():
            logger.info(
                "github signup: %s admitted by login; pin the invite as %s:%s so a later holder "
                "of the login is not",
                user.login,
                user.login,
                user.id,
            )
            return True
    return False


def personal_grant(settings: Settings, user: GitHubUser) -> OrgGrant:
    from felix.auth.github import OrgGrant

    # No org: `org`/`org_id` name where membership was read, and nothing was.
    return OrgGrant(org="", org_id=0, tenant=personal_tenant(user.id), scopes=signup_scopes(settings))


def admit(settings: Settings, user: GitHubUser, restricted: list[str]) -> tuple[list[OrgGrant], list[str]]:
    """For a login no org admitted: the personal grant, or the refusal that says why not.

    A restricted org is left for `choose_grant` to report when the account is not invited: it
    may be the person's own org hiding their membership, and that is the operator's to fix.
    """
    from felix.auth.github import GitHubLoginError, LoginErrorCode

    if is_invited(settings, user):
        if restricted:
            logger.warning(
                "github signup: %s admitted to a personal tenant while orgs hid membership: %s",
                user.login,
                ", ".join(restricted),
            )
        return [personal_grant(settings, user)], []
    if restricted:
        return [], restricted
    raise GitHubLoginError(
        LoginErrorCode.NOT_INVITED, "sign-up is invite-only, and this GitHub account is not on the list"
    )


def admits_personal_claim(payload: dict[str, Any], tenant: str, *, self_issued: bool) -> bool | None:
    """Whether a token may claim the personal tenant `tenant`; None when `tenant` is not one.

    Only a token Felix minted at sign-in may, and only for its own subject: the tenant names the
    GitHub account that owns it, so a `gh-<id>` claim from any other issuer, or for anyone else,
    is a claim on someone else's tenant.
    """
    owner = personal_tenant_owner(tenant)
    if owner is None:
        return None
    return self_issued and payload.get("idp") == "github" and payload.get("sub") == f"github:{owner}"


def validate_signup_config(settings: Settings) -> None:
    """Refuse to start a signup that would admit nobody, admit with admin, or collide."""
    from felix.auth.github import is_enabled

    if not signup_enabled(settings):
        if settings.github_signup_logins.strip() or settings.github_signup_scopes.strip():
            logger.warning(
                "FELIX_GITHUB_SIGNUP_LOGINS/_SCOPES are set but FELIX_GITHUB_SIGNUP is off; "
                "nobody can sign up"
            )
        return
    if not is_enabled(settings):
        raise RuntimeError("FELIX_GITHUB_SIGNUP is on, so FELIX_GITHUB_CLIENT_ID must be set.")
    try:
        invites = parse_signup_logins(settings.github_signup_logins)
    except ValueError as exc:
        raise RuntimeError(f"FELIX_GITHUB_SIGNUP_LOGINS: {exc}") from exc
    if settings.github_signup == "invite" and not invites:
        # Never read as "everyone": an empty list under `invite` is a mistake, not an opening.
        raise RuntimeError(
            "FELIX_GITHUB_SIGNUP=invite needs at least one login in FELIX_GITHUB_SIGNUP_LOGINS."
        )
    try:
        scopes = signup_scopes(settings)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not scopes:
        raise RuntimeError(
            "FELIX_GITHUB_SIGNUP is on, so FELIX_GITHUB_SIGNUP_SCOPES must say what a personal tenant may do."
        )
    if refused := sorted(_REFUSED_SCOPES & set(scopes)):
        raise RuntimeError(f"FELIX_GITHUB_SIGNUP_SCOPES may not grant {refused} to people who sign up.")
    from felix.config import _configured_tenant_ids

    for label, tenant in _configured_tenant_ids(settings):
        if personal_tenant_owner(tenant) is not None:
            raise RuntimeError(
                f"{label} names {tenant!r}, which is a personal tenant's id while signup is on; "
                "it would belong to the GitHub account with that id."
            )
