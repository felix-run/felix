"""SSRF guard for outbound URLs.

The previous version checked private/loopback/link-local ranges **only when the hostname
was already an IP literal**. Any DNS name resolving to `169.254.169.254`, `10.x`, or
`127.0.0.1` sailed through — `*.nip.io`, an attacker-controlled `A` record, or DNS
rebinding — which is the standard cloud-metadata SSRF path. It also decided "is this an
IP?" by string-matching its own exception message, so rewording an error would have
started admitting private addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger("felix.security.ssrf")

# Hostname suffixes that only resolve inside a cluster or cloud environment.
_INTERNAL_SUFFIXES = (
    ".internal",
    ".cluster.local",
    ".svc",
    ".svc.cluster.local",
    ".local",
    ".localdomain",
)

# Bare names with the same problem.
_INTERNAL_NAMES = frozenset(
    {
        "localhost",
        "kubernetes",
        "kubernetes.default",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "consul",
    }
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Reason this address must not be dialled, or None when it is routable."""
    # ::ffff:169.254.169.254 is link-local, but only via .ipv4_mapped — the plain
    # attribute checks return False, so the metadata service was reachable that way.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(ip.ipv4_mapped)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (cloud metadata)"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if isinstance(ip, ipaddress.IPv4Address):
        # Carrier-grade NAT — not private per RFC1918 but never a legitimate target.
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return "carrier-grade NAT"
    return None


def resolve_host(host: str) -> list[str]:
    """Every address a hostname resolves to. Empty when resolution fails."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(str(addr))
    return out


def assert_safe_outbound_url(url: str, *, allow_http: bool = False, resolve: bool = True) -> None:
    """Reject URLs that would reach loopback, link-local, or private space.

    ``resolve`` performs the DNS lookup that makes the check meaningful for hostnames.
    Callers that have already validated and pinned an address can pass ``False``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("url scheme must be http(s)")
    if parsed.scheme == "http" and not allow_http:
        raise ValueError("http urls blocked outside development")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("url has no host")

    if host in _INTERNAL_NAMES and not allow_http:
        raise ValueError(f"internal host blocked: {host}")
    if host.endswith(_INTERNAL_SUFFIXES) and not allow_http:
        raise ValueError(f"internal DNS suffix blocked: {host}")

    literal = _parse_ip_literal(host)
    if literal is not None:
        reason = _is_blocked_ip(literal)
        if reason and not (allow_http and literal.is_loopback):
            raise ValueError(f"blocked address ({reason}): {host}")
        return

    if not resolve:
        return

    addresses = resolve_host(host)
    if not addresses:
        # Do not fail closed on a transient DNS error: the connection will fail anyway,
        # and refusing every lookup failure would make the harness brittle offline.
        logger.debug("ssrf: could not resolve %s; deferring to the connection", host)
        return
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _is_blocked_ip(ip)
        if reason and not (allow_http and ip.is_loopback):
            raise ValueError(f"host {host} resolves to a blocked address ({reason}): {addr}")


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP literal, including the decimal/octal forms getaddrinfo accepts.

    ``http://2130706433/`` is 127.0.0.1 to the resolver but is not an IP literal to
    ``ipaddress``, so it previously fell through to the hostname path unchecked.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Bare integer, e.g. 2130706433 -> 127.0.0.1
    if host.isdigit():
        try:
            return ipaddress.ip_address(int(host))
        except ValueError:
            return None
    return None


class EgressBlocked(ValueError):
    """A dial refused by the egress guard.

    Deliberately carries no resolved address. The detailed reason names the IP a hostname
    resolved to, and at dial time that string lands in a tool message the model reads —
    which turns any peer, container or MCP ref into an internal-DNS oracle. The detail is
    logged instead. Subclasses `ValueError` so existing handlers still catch it.
    """


# `getaddrinfo` inherits resolv.conf, so a blackholed nameserver can hold a thread for tens
# of seconds. A running thread cannot be cancelled, and `to_thread` uses the loop's shared
# default executor — the same pool as GCS, embeddings and the Docker sandbox — so an
# unbounded lookup does not just delay this dial, it starves every other offloaded call.
_RESOLVE_BUDGET_S = 3.0


async def assert_safe_outbound_url_async(url: str, *, allow_http: bool = False) -> None:
    """The resolving check, run off the event loop and on a deadline.

    `resolve_host` is a synchronous `getaddrinfo`. On an async path that blocks every other
    request on the worker for as long as the resolver takes. Use this anywhere the caller
    can await; use `assert_safe_outbound_url(..., resolve=False)` where it cannot, and let
    the dial-time check do the resolving.

    A lookup that exceeds the budget is refused rather than allowed through: this check is
    advisory — whoever dials resolves again — so letting a timeout fall through would hand
    a selectively-slow nameserver exactly the bypass it exists to close.

    Outbound HTTP no longer needs this: `felix.security.egress` pins the connection to an
    address it validated, which is enforcement rather than advice. The remaining caller is
    the browser, where Chromium does its own resolving and cannot be pinned from here.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(assert_safe_outbound_url, url, allow_http=allow_http),
            timeout=_RESOLVE_BUDGET_S,
        )
    except TimeoutError:
        logger.warning("ssrf: resolution exceeded %.0fs for %s; refusing", _RESOLVE_BUDGET_S, url)
        raise EgressBlocked("egress_blocked: destination could not be verified") from None
    except ValueError as exc:
        logger.warning("ssrf: refused %s (%s)", url, exc)
        raise EgressBlocked("egress_blocked: destination not permitted") from None


def assert_safe_outbound_url_for_hosts(
    url: str,
    allowed_hosts: set[str] | frozenset[str],
    *,
    allow_http: bool = False,
) -> None:
    """SSRF check with an explicit host allow-list."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise ValueError(f"host not in allow-list: {host}")
    assert_safe_outbound_url(url, allow_http=allow_http)


__all__ = [
    "EgressBlocked",
    "assert_safe_outbound_url",
    "assert_safe_outbound_url_async",
    "assert_safe_outbound_url_for_hosts",
    "resolve_host",
]
