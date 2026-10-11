"""Credentials the model never holds: an operator's map from host to secret, attached in transit.

A model can drive `http_fetch` at an authenticated API only if something adds the key, and every
place a model could put it -- a tool argument, a URL, a header it writes -- is a place it can also
copy it from. So the key never reaches the model. The operator registers it once
(`FELIX_EGRESS_CREDENTIALS`), per tenant, against one exact host and a path prefix:

    {"github": {"host": "api.github.com", "path_prefix": "/repos/acme/",
                "secret": "secret:GITHUB_TOKEN", "scheme": "Bearer", "tenants": ["acme"]}}

A manifest names the ids it may use (`spec.network.credentials`), and must also set
`network.allow_hosts` with each credential's host on it: an agent carrying authority has bounded
egress. The guarded transport (`security/egress.py`) attaches the header after the host check, to
a request -- each redirect hop judged again -- whose host is the credential's, whose path is inside
its prefix (`within_prefix`), and whose scheme is https. The value is resolved when first sent and
registered for masking, so a server that echoes it back is redacted like any secret. The model,
the tool's arguments and every log see only the request it made.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from felix.security.host_allowlist import normalise_host_pattern, within_prefix

logger = logging.getLogger("felix.security.egress_credentials")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# A header name is an RFC 9110 token; these are the transport's, not a credential's.
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer", "cookie"}
)


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
        return self.tenants is None or tenant_id in self.tenants

    def covers(self, url: httpx.URL, *, allow_http: bool) -> bool:
        """Whether a request to `url` is one this credential is sent with: its host, its port, a
        path inside its prefix, over https (http only where plain-text dials are allowed)."""
        if url.scheme != "https" and not (allow_http and url.scheme == "http"):
            return False
        # The prefix is the credential's own origin, so `within_prefix` refuses another host or port
        # as well as another path.
        port = f":{self.port}" if self.port is not None else ""
        return within_prefix(str(url), f"{url.scheme}://{self.host}{port}{self.path_prefix}")


def parse_egress_credentials(settings: Any) -> dict[str, EgressCredential]:
    """`FELIX_EGRESS_CREDENTIALS`, validated. `ValueError` naming what is wrong."""
    raw = str(getattr(settings, "egress_credentials", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict):
        raise ValueError("must be a JSON object of credential id -> {host, secret, tenants}")
    out: dict[str, EgressCredential] = {}
    for name, spec in parsed.items():
        if not isinstance(name, str) or not _ID_RE.match(name):
            raise ValueError(f"credential id {name!r} must match {_ID_RE.pattern}")
        if not isinstance(spec, dict):
            raise ValueError(f"credential {name!r} must be an object")
        out[name] = _credential(name, spec)
    return out


def _credential(name: str, spec: dict[str, Any]) -> EgressCredential:
    host = spec.get("host")
    if not isinstance(host, str) or not host:
        raise ValueError(f"credential {name!r} needs a `host`")
    host = normalise_host_pattern(host)
    if host.startswith("*."):
        # A wildcard would send the key to every name under it, one of which may not be the API.
        raise ValueError(f"credential {name!r} `host` must be one exact host, not a wildcard")
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
    if not isinstance(scheme, str) or not re.fullmatch(r"[A-Za-z0-9-]{0,32}", scheme):
        raise ValueError(f"credential {name!r} `scheme` must be a word such as Bearer, or empty")
    secret = spec.get("secret")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError(f"credential {name!r} needs a `secret` (secret:NAME, or a literal)")
    # Required, with "*" for every tenant: a credential open to all has to be a written decision.
    tenants = spec.get("tenants")
    if tenants != "*" and not (
        isinstance(tenants, list) and tenants and all(isinstance(t, str) for t in tenants)
    ):
        raise ValueError(f'credential {name!r} `tenants` must be a list of tenant ids, or "*" for all')
    return EgressCredential(
        name=name,
        host=host,
        port=port,
        path_prefix=prefix,
        header=header,
        scheme=scheme,
        secret=secret,
        tenants=None if tenants == "*" else frozenset(tenants),
    )


@dataclass
class CredentialSet:
    """The credentials one compiled agent may send, and the values resolved so far. Held by the
    tool, not the model: nothing here is ever in a prompt, an argument or a result."""

    credentials: tuple[EgressCredential, ...]
    settings: Any
    allow_http: bool = False
    _values: dict[str, str] = field(default_factory=dict, repr=False)

    async def attach(self, request: httpx.Request) -> None:
        """Set the matching credential's header on `request`, replacing whatever was there."""
        for cred in self.credentials:
            if not cred.covers(request.url, allow_http=self.allow_http):
                continue
            value = await self._value(cred)
            request.headers[cred.header] = f"{cred.scheme} {value}" if cred.scheme else value
            logger.info("egress credential attached credential=%s host=%s", cred.name, cred.host)
            return

    async def _value(self, cred: EgressCredential) -> str:
        if cred.name not in self._values:
            from felix.secrets import build_secrets, register_resolved_secret, resolve_secret_value

            value = await resolve_secret_value(build_secrets(self.settings), cred.secret)
            if not value:
                raise ValueError(f"egress credential {cred.name} resolved to nothing")
            # Masked wherever it could surface: a server that echoes the header back, a log line.
            register_resolved_secret(value)
            self._values[cred.name] = value
        return self._values[cred.name]


def bind_credentials(
    names: list[str], settings: Any, tenant_id: str, *, allow_hosts: list[str] | None, allow_http: bool
) -> CredentialSet | None:
    """The `CredentialSet` a manifest's `network.credentials` grants, or `ValueError` for an id
    the operator has not registered for this tenant or a host the manifest's list leaves out."""
    if not names:
        return None
    registry = parse_egress_credentials(settings)
    chosen: list[EgressCredential] = []
    for name in names:
        cred = registry.get(name)
        if cred is None or not cred.allows(tenant_id):
            raise ValueError(f"unknown egress credential: {name}")
        if allow_hosts is None or cred.host not in allow_hosts:
            raise ValueError(
                f"egress credential {name} is for {cred.host}, which network.allow_hosts must name exactly"
            )
        chosen.append(cred)
    return CredentialSet(tuple(chosen), settings, allow_http=allow_http)


__all__ = [
    "CredentialSet",
    "EgressCredential",
    "bind_credentials",
    "parse_egress_credentials",
]
