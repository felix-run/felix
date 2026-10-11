"""`spec.network.allow_hosts`: the hosts an agent's tools may reach, and the bound they make.

The SSRF guard (`security/egress.py`) answers "is this address safe for anyone to reach"; this
answers "is this host one *this agent* may reach". It is applied where the model chooses the
destination -- `http_fetch` (every redirect hop, in the guarded transport) and the browser (the
navigation and every request the page makes) -- and checked against every URL the manifest itself
declares (MCP servers, A2A peers, container gateways, fetch and browser prefixes), so an agent's
reachable hosts are the list and nothing else.

A pattern is a host (`api.github.com`), a subdomain wildcard (`*.github.com`: any name under it,
never the apex), or an IP literal. No scheme, port or path: those are other grammars, and a
pattern that carried one would match nothing. Patterns are ASCII; an internationalised name is
written in its punycode form, the one a request's URL carries.

A child agent answers to its parent's list as well as its own: an `EgressBound` is every list
from the agent up to the request's root, and a host passes only if each of them allows it. A
background `task` child runs in the worker, outside its parent's compile, so its parents' lists
ride the durable run and are restored onto the run's context (`EGRESS_CEILINGS_EXTRA`).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# The lists above a background child, restored from its durable run: `list[list[str]]`.
EGRESS_CEILINGS_EXTRA = "egress_ceilings"

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")


def normalise_host_pattern(raw: str) -> str:
    """`raw` as a stored pattern -- lowercased, without a trailing dot -- or `ValueError`."""
    text = raw.strip().lower().rstrip(".")
    if not text:
        raise ValueError("an allow_hosts entry must not be empty")
    try:
        return str(ipaddress.ip_address(text.strip("[]")))
    except ValueError:
        pass
    name = text[2:] if text.startswith("*.") else text
    if not _HOST_RE.match(name) or len(name) > 253:
        raise ValueError(
            f"allow_hosts entry {raw!r} is not a host name, '*.' and a host name, or an IP address "
            "(no scheme, port or path; punycode for an internationalised name)"
        )
    if text.startswith("*.") and "." not in name:
        raise ValueError(f"allow_hosts entry {raw!r} would match every name under a top-level domain")
    return text


def _normalise_host(host: str) -> str:
    text = host.strip().lower().rstrip(".").strip("[]")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


@dataclass(frozen=True, slots=True)
class HostAllowlist:
    """One agent's `allow_hosts`, already normalised."""

    patterns: tuple[str, ...]

    def allows(self, host: str) -> bool:
        name = _normalise_host(host)
        if not name:
            return False
        for pattern in self.patterns:
            if pattern.startswith("*."):
                if name.endswith(pattern[1:]) and len(name) > len(pattern) - 1:
                    return True
            elif name == pattern:
                return True
        return False


@dataclass(frozen=True, slots=True)
class EgressBound:
    """Every `allow_hosts` list from an agent up to its root; empty means unrestricted."""

    lists: tuple[HostAllowlist, ...] = ()

    def under(self, patterns: Sequence[str] | None) -> EgressBound:
        """This bound with one more agent's list beneath it; `None` adds no restriction."""
        if patterns is None:
            return self
        return EgressBound((*self.lists, HostAllowlist(tuple(patterns))))

    def allows(self, host: str) -> bool:
        """Whether every list in the bound, and every one the run carries, allows `host`."""
        return all(lst.allows(host) for lst in (*self.lists, *run_ceilings()))

    def as_state(self) -> list[list[str]]:
        """The bound as a durable run stores it, for a background child to restore."""
        return [list(lst.patterns) for lst in (*self.lists, *run_ceilings())]


def run_ceilings() -> tuple[HostAllowlist, ...]:
    """The lists the current run inherited from a background child's parents, if any."""
    from felix.context import try_get_context

    req = try_get_context()
    raw = req.extras.get(EGRESS_CEILINGS_EXTRA) if req is not None else None
    if not raw:
        return ()
    return tuple(HostAllowlist(tuple(str(p) for p in lst)) for lst in raw)


def restore_ceilings(raw: object) -> list[list[str]]:
    """A durable run's stored lists, re-validated: the row is data, not trusted input."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(lst, list) for lst in raw):
        raise ValueError("durable run egress_ceilings is not a list of lists")
    return [[normalise_host_pattern(str(p)) for p in lst] for lst in raw]


def url_host(url: str) -> str:
    """The host of `url` in the spelling patterns use: ASCII (punycode), as a request carries it.
    Empty for a URL with none, or one that does not parse."""
    import httpx

    try:
        return httpx.URL(url).raw_host.decode("ascii", errors="replace")
    except httpx.InvalidURL, TypeError, ValueError:
        return ""


def hosts_outside(bound: HostAllowlist | EgressBound, urls: Iterable[str]) -> list[str]:
    """The hosts of `urls` that `bound` refuses, for a refusal naming them: one manifest's own
    list at write, or the whole bound -- parents and run included -- at compile."""
    out: list[str] = []
    for url in urls:
        host = url_host(url)
        if url and not bound.allows(host):
            out.append(host or url)
    return out


__all__ = [
    "EGRESS_CEILINGS_EXTRA",
    "EgressBound",
    "HostAllowlist",
    "hosts_outside",
    "normalise_host_pattern",
    "restore_ceilings",
    "run_ceilings",
    "url_host",
]
