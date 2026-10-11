"""Credentials the model never holds: an operator's map from host to secret, attached in transit.

A model can drive `http_fetch` at an authenticated API only if something adds the key, and every
place a model could put it -- a tool argument, a URL, a header it writes -- is a place it can also
copy it from. So the key never reaches the model. The operator registers it once
(`FELIX_EGRESS_CREDENTIALS`), per tenant, against one exact host and a path prefix:

    {"github": {"host": "api.github.com", "path_prefix": "/repos/acme/",
                "secret": "secret:GITHUB_TOKEN", "scheme": "Bearer", "tenants": ["acme"]}}

A manifest names the ids it may use (`spec.network.credentials`), and must also set
`network.allow_hosts` with each credential's host on it: an agent carrying authority has bounded
egress. The value is resolved when the agent compiles -- before the masking wrappers take their
list -- and registered for masking, so a server that echoes it back is redacted like any secret.
The guarded transport (`security/egress.py`) sets the header after the host check on a request
whose origin is the credential's, whose path is inside its prefix (`within_prefix`) and whose
scheme is https, and strips it from any request no credential covers; each redirect hop is a
request of its own, judged again. The model, the tool's arguments and every log see only the
request it made.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from felix.security.host_allowlist import normalise_host_pattern, within_prefix
from felix.settings_registry import parse_registry, parse_tenants, tenant_allowed

logger = logging.getLogger("felix.security.egress_credentials")

# A header name is an RFC 9110 token; these are the transport's, not a credential's.
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer", "cookie"}
)
_SCHEME_RE = re.compile(r"^[A-Za-z0-9-]{0,32}$")
# What a value may be: visible ASCII and inner spaces, at least as long as masking will match. A
# trailing newline from `echo key | ... --data-file=-` would otherwise reach the wire, be refused
# there, and land in a log line inside the error that names it.
_VALUE_RE = re.compile(r"[!-~](?:[ -~]*[!-~])?")
_MIN_VALUE_CHARS = 8


class EgressCredentialError(ValueError):
    """A credential that could not be bound: its message names the id, never the value."""


@dataclass(frozen=True, slots=True)
class EgressCredential:
    """One registered credential: which requests get which header, from which secret."""

    name: str
    host: str
    # None is the scheme's default port: a request to another port on the host is not the API.
    port: int | None
    path_prefix: str
    header: str
    scheme: str
    secret: str
    tenants: frozenset[str] | None

    def allows(self, tenant_id: str) -> bool:
        return tenant_allowed(self.tenants, tenant_id)

    def covers(self, url: httpx.URL, *, allow_http: bool) -> bool:
        """Whether a request to `url` is one this credential is sent with: its host and port, a
        path inside its prefix, over https (http only where plain-text dials are allowed)."""
        if url.scheme != "https" and not (allow_http and url.scheme == "http"):
            return False
        # A doubly-encoded path is one a server that decodes twice could walk out of the prefix.
        if b"%25" in url.raw_path.split(b"?", 1)[0]:
            return False
        # The prefix is the credential's own origin, so `within_prefix` refuses another host or port
        # as well as another path.
        port = f":{self.port}" if self.port is not None else ""
        return within_prefix(str(url), f"{url.scheme}://{self.host}{port}{self.path_prefix}")


def parse_egress_credentials(settings: Any) -> dict[str, EgressCredential]:
    """`FELIX_EGRESS_CREDENTIALS`, validated. `ValueError` naming what is wrong."""
    parsed = parse_registry(
        str(getattr(settings, "egress_credentials", "") or ""),
        kind="credential",
        shape="{host, secret, tenants}",
    )
    return {name: _credential(name, spec) for name, spec in parsed.items()}


def _credential(name: str, spec: dict[str, Any]) -> EgressCredential:
    host = spec.get("host")
    if not isinstance(host, str) or not host:
        raise ValueError(f"credential {name!r} needs a `host`")
    host = normalise_host_pattern(host)
    if host.startswith("*."):
        # A wildcard would send the key to every name under it, one of which may not be the API.
        raise ValueError(f"credential {name!r} `host` must be one exact host, not a wildcard")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"credential {name!r} `host` must be a name, not an IP address")
    port = spec.get("port")
    if port is not None and not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        raise ValueError(f"credential {name!r} `port` must be a port number, or absent for the default")
    prefix = spec.get("path_prefix", "/")
    if not isinstance(prefix, str) or not prefix.startswith("/") or "?" in prefix or "#" in prefix:
        raise ValueError(f"credential {name!r} `path_prefix` must be a path starting with '/'")
    header = spec.get("header", "Authorization")
    if not isinstance(header, str) or not _HEADER_RE.match(header) or header.lower() in _FORBIDDEN_HEADERS:
        raise ValueError(
            f"credential {name!r} `header` must be a header name other than {sorted(_FORBIDDEN_HEADERS)}"
        )
    scheme = spec.get("scheme", "")
    if not isinstance(scheme, str) or not _SCHEME_RE.match(scheme):
        raise ValueError(f"credential {name!r} `scheme` must match {_SCHEME_RE.pattern}, such as Bearer")
    secret = spec.get("secret")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError(f"credential {name!r} needs a `secret` (secret:NAME, or a literal)")
    return EgressCredential(
        name=name,
        host=host,
        port=port,
        path_prefix=prefix,
        header=header,
        scheme=scheme,
        secret=secret,
        tenants=parse_tenants(spec, kind="credential", name=name),
    )


@dataclass
class CredentialSet:
    """The credentials one compiled agent may send, their values resolved at compile. Held by the
    tool's transport, not the model: nothing here is in a prompt, an argument or a result."""

    credentials: tuple[EgressCredential, ...]
    _values: dict[str, str] = field(default_factory=dict, repr=False)

    def attach(self, request: httpx.Request, *, allow_http: bool) -> None:
        """Set the covering credential's header on `request`, and strip every bound credential's
        header from it otherwise: a client that copies headers onto a redirect hop -- httpx does,
        within an origin, and for any header but `Authorization` across one -- must not carry a
        key to a request the credential does not cover."""
        covering = next((c for c in self.credentials if c.covers(request.url, allow_http=allow_http)), None)
        for cred in self.credentials:
            if cred is not covering and cred.header in request.headers:
                del request.headers[cred.header]
        if covering is None:
            return
        value = self._values[covering.name]
        request.headers[covering.header] = f"{covering.scheme} {value}" if covering.scheme else value
        logger.info("egress credential attached credential=%s host=%s", covering.name, covering.host)


async def bind_credentials(
    names: list[str], settings: Any, tenant_id: str, *, allow_hosts: list[str] | None
) -> CredentialSet | None:
    """The `CredentialSet` a manifest's `network.credentials` grants, each value resolved and
    registered for masking now -- before the compile's masking wrappers take their list.
    `EgressCredentialError` for an id the operator has not registered for this tenant, a host the
    manifest's list does not name, or a value that cannot be resolved or would not be masked."""
    if not names:
        return None
    from felix.secrets import build_secrets, register_resolved_secret, resolve_secret_value

    registry = parse_egress_credentials(settings)
    chosen: list[EgressCredential] = []
    values: dict[str, str] = {}
    for name in names:
        cred = registry.get(name)
        # One message whether the id is unregistered or another tenant's: which ids exist is not
        # this tenant's to learn.
        if cred is None or not cred.allows(tenant_id):
            raise EgressCredentialError(f"unknown egress credential: {name}")
        if allow_hosts is None or cred.host not in allow_hosts:
            raise EgressCredentialError(
                f"egress credential {name} is for {cred.host}, which network.allow_hosts must name exactly"
            )
        try:
            value = await resolve_secret_value(build_secrets(settings), cred.secret)
        except Exception:
            # The backend's own text is the operator's; the compile is refused by id.
            logger.warning("egress credential unresolved credential=%s", name, exc_info=True)
            raise EgressCredentialError(f"egress credential {name} could not be resolved") from None
        # `fullmatch`: `$` matches before a trailing newline, which is the value this refuses.
        if len(value) < _MIN_VALUE_CHARS or not _VALUE_RE.fullmatch(value):
            raise EgressCredentialError(
                f"egress credential {name} resolved to a value that is empty, under "
                f"{_MIN_VALUE_CHARS} characters, or holds whitespace at its ends or a control character"
            )
        register_resolved_secret(value)
        chosen.append(cred)
        values[name] = value
    return CredentialSet(tuple(chosen), values)


__all__ = [
    "CredentialSet",
    "EgressCredential",
    "EgressCredentialError",
    "bind_credentials",
    "parse_egress_credentials",
]
