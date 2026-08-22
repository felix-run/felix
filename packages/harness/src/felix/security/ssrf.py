"""SSRF allow-list for outbound URLs."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def assert_safe_outbound_url(url: str, *, allow_http: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("url scheme must be http(s)")
    if parsed.scheme == "http" and not allow_http:
        raise ValueError("http urls blocked outside development")
    host = parsed.hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"} and not allow_http:
        raise ValueError("loopback blocked")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("private IP blocked")
    except ValueError as e:
        if "private" in str(e) or "blocked" in str(e):
            raise
        # hostname not an IP — ok
        pass
    if host.endswith((".internal", ".cluster.local")):
        raise ValueError("internal DNS suffix blocked")


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
