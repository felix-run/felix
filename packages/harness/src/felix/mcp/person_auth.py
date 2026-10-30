"""`mcp_servers[].auth: person:<provider>`: call a remote MCP server as the person the run acts for.

At compile, each such server is given the access token of the person the compile is for
(`BuildDeps.memory_person`, from `AuthContext.person`): from the connection vault
(`felix.auth.connections`, provider `google` and any other registered one), or for `person:github`
from the GitHub connection of a person who signed in with GitHub (`felix.auth.github_connections`,
keyed by their GitHub user id, which their person key carries as `github:<id>`). Never an
operator's credential, never another person's.

A run that acts for no one person -- anonymous, a shared key, a trigger -- and a person with no
such connection get none of that server's tools, said in the log rather than failing the compile:
the agent still serves them, without the server. The tools are discovered with the person's own
token, so what each person sees is what the server shows them, and the discovery cache is keyed by
the person, never the token (`mcp.client._discovery_key`). Every token opened here is added to the
compile's secret masking, which is per agent and so per request.

Only to a server `FELIX_MCP_PERSON_AUTH` allows for the provider and tenant (`person_policy`): a
manifest is written by whoever holds `manifests:write`, not by the people whose tokens it sends.

A token is opened once per compile. A run that outlives it -- an hour for Google, eight for GitHub
-- finds the server refusing it, as any expired credential is refused.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from felix.logging_setup import loggable
from felix.manifests.schema import McpServerRef

logger = logging.getLogger("felix.mcp.person_auth")

GITHUB = "github"
NO_PERSON, NOT_CONNECTED, NOT_ALLOWED = "no_person", "not_connected", "not_allowed"
#: The tag the prompt note is wrapped in.
PERSON_SOURCES_TAG = "person_sources"
_UNSAFE = re.compile(r"[<>`\r\n]")
# What `felix.auth.github.mint_login_token` writes: `github:` and the user's numeric id.
_GITHUB_PERSON = re.compile(r"github:([1-9][0-9]{0,19})", re.ASCII)


@dataclass(slots=True)
class PersonRefs:
    """The refs to bind (person auth replaced by the person's token), the tokens to mask, and the
    identity each person-auth server's discovery is cached under."""

    refs: list[McpServerRef]
    tokens: list[str] = field(default_factory=list)
    #: Per ref in `refs`, by position (server names need not be unique): `tenant|person` for a
    #: person-auth server, `""` for any other.
    identities: list[str] = field(default_factory=list)
    #: (server, provider, reason) for each person-auth server left out: `no_person`,
    #: `not_connected` or `not_allowed` -- what the model is told (`left_out_note`).
    left_out: list[tuple[str, str, str]] = field(default_factory=list)


def _github_user_id(person: str) -> int | None:
    """The GitHub user id of a person who signed in to Felix with GitHub, or None. Only Felix's own
    sign-in (`felix-self`) mints `github:<id>`; another issuer -- an API key, a remote IdP -- whose
    subject reads the same is someone else (`felix.auth.connections`). Plain ASCII digits only:
    `str.isdigit` takes `٤٢`, and `int` reads it as 42."""
    from felix.auth.jwt import SELF_ISSUER

    issuer, _, subject = person.partition("|")
    match = _GITHUB_PERSON.fullmatch(subject)
    return int(match.group(1)) if issuer == SELF_ISSUER and match else None


async def _token(settings: Any, tenant_id: str, person: str, provider: str) -> str | None:
    """The person's access token for `provider`, or None (with the reason logged)."""
    if provider == GITHUB:
        from felix.auth import github_connections
        from felix.auth.github import GitHubLoginError

        user_id = _github_user_id(person)
        if user_id is None:
            logger.info("mcp person auth: %s did not sign in with GitHub", loggable(person, limit=200))
            return None
        try:
            return await github_connections.access_token(settings, tenant_id, user_id)
        except github_connections.GitHubNotConnected, github_connections.GitHubConnectionRevoked:
            logger.info("mcp person auth: %s has no working GitHub connection", loggable(person, limit=200))
        except GitHubLoginError as exc:
            logger.warning("mcp person auth: GitHub could not be reached error=%s", type(exc).__name__)
        except Exception as exc:
            # A sealed row that no longer opens (a rotated key), the store down: the server is left
            # out, and the rest of the agent still serves the person.
            logger.warning("mcp person auth: GitHub token could not be opened error=%s", type(exc).__name__)
        return None
    from felix.auth import connections

    try:
        return await connections.access_token(settings, tenant_id, person, provider)
    except (connections.NotConnected, connections.ConnectionRevoked, connections.AmbiguousConnection) as exc:
        # Ambiguous: several accounts of the provider, and a manifest cannot say whose.
        logger.info(
            "mcp person auth: no usable %s connection for %s (%s)",
            loggable(provider, limit=40),
            loggable(person, limit=200),
            type(exc).__name__,
        )
    except Exception as exc:
        logger.warning(
            "mcp person auth: %s token could not be opened error=%s",
            loggable(provider, limit=40),
            type(exc).__name__,
        )
    return None


async def open_person_refs(
    refs: list[McpServerRef], settings: Any, tenant_id: str, person: str
) -> PersonRefs:
    """`refs`, each `auth: person:<provider>` one given `person`'s token or left out. `person` is
    the compile's (`BuildDeps.memory_person`, from the auth it compiles for), never ambient."""
    if not any(r.person_provider for r in refs):
        return PersonRefs(list(refs), identities=[""] * len(refs))
    from felix.mcp.person_policy import person_auth_permitted

    out = PersonRefs([])
    for ref in refs:
        provider = ref.person_provider
        if not provider:
            out.refs.append(ref)
            out.identities.append("")
            continue
        if settings is None or not person_auth_permitted(provider, ref.url, tenant_id, settings):
            # Checked when the manifest was stored; again here, for a row stored before the
            # operator withdrew the server, or a bundled file the store never saw.
            logger.warning(
                "mcp person auth: %s left out: FELIX_MCP_PERSON_AUTH does not allow its server for %s",
                loggable(ref.name, limit=80),
                loggable(provider, limit=40),
            )
            out.left_out.append((ref.name, provider, NOT_ALLOWED))
            continue
        token = (
            await _token(settings, tenant_id, person, provider) if person and settings is not None else None
        )
        if token is None:
            if not person:
                logger.info(
                    "mcp person auth: %s left out: this run acts for no one person",
                    loggable(ref.name, limit=80),
                )
            out.left_out.append((ref.name, provider, NOT_CONNECTED if person else NO_PERSON))
            continue
        out.refs.append(ref.model_copy(update={"auth": token}))
        out.tokens.append(token)
        out.identities.append(f"{tenant_id}|{person}")
    return out


def left_out_note(left_out: list[tuple[str, str, str]]) -> str:
    """What the model is told about the person-auth servers this compile left out, or `""`. Without
    it, an agent asked about the person's GitHub finds no GitHub tools and answers as if there
    were nothing there; with it, it can say what the person would do about it."""
    if not left_out:
        return ""

    def clean(text: str) -> str:
        return _UNSAFE.sub("", text)

    reasons = {
        NO_PERSON: "this run acts for no one person, so there is no account to use",
        NOT_CONNECTED: "the person has not connected their {provider} account; they can connect it in Felix",
        NOT_ALLOWED: "this deployment does not allow it to be sent anyone's {provider} token",
    }
    lines = [
        f"- `{clean(name)}`: " + reasons[reason].format(provider=clean(provider))
        for name, provider, reason in left_out
    ]
    return (
        f"<{PERSON_SOURCES_TAG}>These tool sources act as the person, and their tools are missing from "
        "this run. What they would have shown is unknown, not empty: say so rather than answer as if "
        "there were nothing.\n" + "\n".join(lines) + f"</{PERSON_SOURCES_TAG}>"
    )


__all__ = ["PERSON_SOURCES_TAG", "PersonRefs", "left_out_note", "open_person_refs"]
