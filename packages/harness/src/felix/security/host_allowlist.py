"""`spec.network.allow_hosts`: the hosts an agent's tools may reach, and the bound they make.

The SSRF guard (`security/egress.py`) answers "is this address safe for anyone to reach"; this
answers "is this host one *this agent* may reach". It is enforced where the model chooses the
destination -- `http_fetch`, every redirect hop included, in the guarded transport -- and checked
against every URL the manifest itself declares (`Spec.declared_urls`). `browser_tools` are refused
under a list (`Spec._declared_hosts_are_allowed`, `builder._check_declared_egress`): a page opens
sockets no request interceptor sees, so a browser cannot be held to one. It does not reach a
process that opens its own sockets (shell, sandbox, stdio MCP), nor what a remote MCP server or A2A
peer does on the far side: `deploy/GOVERNANCE.md` lists what it does not cover.

A pattern is a host (`api.github.com`), a subdomain wildcard (`*.github.com`: any name under it,
never the apex), or an IP literal. No scheme, port or path: those are other grammars, and a
pattern that carried one would match nothing. Patterns are ASCII; an internationalised name is
written in its punycode form, the one a request's URL carries.

A child agent answers to its parent's list as well as its own: an `EgressBound` is every list
from the agent up to the request's root, and a host passes only if each of them allows it. A
background `task` child compiles in the worker, outside its parent's compile, so its parents'
lists ride the durable run (`as_state`), are put on the run's context
(`EGRESS_CEILINGS_EXTRA`), and seed the root of that compile (`EgressBound.from_state`).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from felix.security.ssrf import BLOCKED_MESSAGE, EgressBlocked

logger = logging.getLogger("felix.security.host_allowlist")

# A background child's inherited lists, restored from its durable run: `list[list[str]]`.
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
    if text.startswith("*."):
        if "." not in name:
            raise ValueError(f"allow_hosts entry {raw!r} would match every name under a top-level domain")
        if all(label.isdigit() for label in name.split(".")):
            raise ValueError(f"allow_hosts entry {raw!r} is a wildcard over an address, not a name")
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
                # A label before the suffix, never the suffix alone (`.example.com`).
                if name.endswith(pattern[1:]) and name != pattern[1:]:
                    return True
            elif name == pattern:
                return True
        return False


@dataclass(frozen=True, slots=True)
class EgressBound:
    """Every `allow_hosts` list from an agent up to its root; empty means unrestricted.

    A value: it reads nothing ambient, so two bounds that compare equal allow the same hosts
    wherever they are asked -- in the guarded transport, a durable run, or a compile memo.
    """

    lists: tuple[HostAllowlist, ...] = ()

    @classmethod
    def from_state(cls, raw: object) -> EgressBound:
        """The bound a durable run stored (`as_state`), re-validated: the row is data, not trusted
        input. `ValueError` for anything that is not a list of lists of patterns."""
        if raw is None:
            return cls()
        if not isinstance(raw, list) or not all(isinstance(lst, list) for lst in raw):
            raise ValueError("durable run egress_ceilings is not a list of lists")
        return cls(tuple(HostAllowlist(tuple(normalise_host_pattern(str(p)) for p in lst)) for lst in raw))

    @property
    def restricted(self) -> bool:
        return bool(self.lists)

    def under(self, patterns: Sequence[str] | None) -> EgressBound:
        """This bound with one more agent's list beneath it; `None` adds no restriction."""
        if patterns is None:
            return self
        return EgressBound((*self.lists, HostAllowlist(tuple(patterns))))

    def allows(self, host: str) -> bool:
        """Whether every list in the bound allows `host`."""
        return all(lst.allows(host) for lst in self.lists)

    def as_state(self) -> list[list[str]]:
        """The bound as a durable run stores it, root first, for a background child to restore."""
        return [list(lst.patterns) for lst in self.lists]


def run_bound() -> EgressBound:
    """The bound the current run inherited -- a background child's parents' lists -- or none.
    Read once, by the root of a compile, never by a check."""
    from felix.context import try_get_context

    req = try_get_context()
    raw = req.extras.get(EGRESS_CEILINGS_EXTRA) if req is not None else None
    return raw if isinstance(raw, EgressBound) else EgressBound()


def url_host(url: str) -> str:
    """The host of `url` in the spelling patterns use: ASCII (punycode), as a request carries it.
    Empty for a URL with none, or one that does not parse."""
    import httpx

    try:
        return httpx.URL(url).raw_host.decode("ascii", errors="replace")
    except httpx.InvalidURL, TypeError, ValueError:
        return ""


def check_host(bound: EgressBound, host: str) -> None:
    """`EgressBlocked` when `bound` refuses `host` -- the one check every enforcing call site
    runs, so they log, count and refuse alike. Never resolves: run it before anything that does,
    or the lookup itself leaks the refused name to its nameserver."""
    from felix.observability.metrics import record_counter

    if bound.allows(host):
        return
    from felix.logging_setup import loggable

    logger.warning("egress blocked host=%s reason=not in allow_hosts", loggable(host, limit=260))
    record_counter("felix_egress_blocked", {"reason": "not in allow_hosts"})
    raise EgressBlocked(BLOCKED_MESSAGE)


def within_prefix(url: str, prefix: str) -> bool:
    """Is `url` inside `prefix`, compared as the request httpx will send rather than as text?

    Origins as origins: scheme, ASCII host and port, so `https://docs.felix.run.evil.com/` and
    `https://evil.com@...` cannot pass for a `https://docs.felix.run/` prefix by sharing its
    characters. The path as sent: httpx removes `..` segments before it sends, so
    `/repos/org/../../evil` is `/evil` and is compared as that; an encoded dot segment or slash
    (`%2e%2e`, `%2f`), which a server may resolve after the comparison, is refused outright. httpx
    reads an empty path as `/`.
    """
    import httpx

    try:
        u, p = httpx.URL(url), httpx.URL(prefix)
    except httpx.InvalidURL, TypeError, ValueError:
        return False
    if (u.scheme, u.raw_host.lower(), u.port) != (p.scheme, p.raw_host.lower(), p.port):
        return False
    raw = u.raw_path.split(b"?", 1)[0].lower()
    if b"%2f" in raw or b"%5c" in raw or b"\\" in raw:
        return False
    # `..;` and `.;` are dot segments to Tomcat and Spring, which drop what follows the `;`.
    if any(seg in (".", "..") or seg.startswith(("..;", ".;")) for seg in u.path.split("/")):
        return False
    return u.path.startswith(p.path)


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
    "check_host",
    "hosts_outside",
    "normalise_host_pattern",
    "run_bound",
    "url_host",
    "within_prefix",
]
