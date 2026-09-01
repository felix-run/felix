"""Browser tools from ``spec.browser_tools`` — Playwright extra, SSRF-guarded."""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import BrowserToolRef
from felix.observability.metrics import record_counter
from felix.security.egress import approved_addresses
from felix.security.ssrf import EgressBlocked, assert_safe_outbound_url_async
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)

logger = logging.getLogger("felix.tools.browser")

_MAX_INLINE = 32_000


class BrowserUrlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, description="http(s) URL to open.")


def _load_playwright() -> Any:
    from playwright.async_api import async_playwright

    return async_playwright


# A hostname goes into a Chromium command-line argument, and `--host-resolver-rules` is a
# comma-separated list. A host containing a comma would append rules of its own — e.g.
# `evil.com,MAP * 169.254.169.254` — so the value is matched against a strict pattern before
# it is interpolated, rather than trusted because it came from urlparse.
_SAFE_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def ipaddress_is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _resolver_rule(host: str, address: str) -> str:
    """A single `MAP host address` rule, with IPv6 bracketed as Chromium expects."""
    literal = ipaddress.ip_address(address)
    target = f"[{address}]" if literal.version == 6 else address
    return f"MAP {host} {target}"


class _BrowserExecutor:
    transport = "browser"

    def __init__(
        self,
        *,
        op: str,
        timeout_ms: int,
        path_prefix: str,
        allow_http: bool,
        binding: str,
    ) -> None:
        self._op = op
        self._timeout_ms = timeout_ms
        self._path_prefix = path_prefix.rstrip("/")
        self._allow_http = allow_http
        self._binding = binding or "chromium"

    async def _check_egress(self, url: str) -> None:
        """SSRF check for any request the page makes, including redirect hops.

        Async because it resolves: this fires once per subresource on a model-supplied URL,
        so a host whose nameserver blackholes queries would otherwise block the event loop
        per request — the same defect as resolving in a pydantic validator, on a hotter path.
        """
        await assert_safe_outbound_url_async(url, allow_http=self._allow_http)

    async def _check_url(self, url: str) -> None:
        """Checks for the top-level navigation the model asked for."""
        await self._check_egress(url)
        if self._path_prefix and not url.startswith(self._path_prefix):
            raise ValueError(f"url must start with {self._path_prefix!r}")

    async def _install_egress_guard(self, page: Any) -> None:
        """Re-validate every request the page makes.

        The URL was checked once and then handed to `page.goto()`, but Chromium follows
        3xx redirects, loads subresources, and runs JS — and the URL is model-supplied.
        So a page that passed the check could 302 to the cloud metadata service and, with
        `op: "content"`, hand the body back to the model. Every other outbound client in
        the codebase sets `follow_redirects=False`; the browser was the one exception.
        """

        async def _guard(route: Any, request: Any) -> None:
            try:
                await self._check_egress(request.url)
            except ValueError as exc:
                logger.warning("browser blocked egress to %s (%s)", request.url, exc)
                record_counter("felix_browser_egress_blocked", {"reason": str(exc)[:40]})
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", _guard)

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        url = str(args.get("url") or "")
        try:
            await self._check_url(url)
        except ValueError as exc:
            return f"browser_error: {exc}"
        try:
            async_playwright = _load_playwright()
        except ImportError:
            return (
                "browser_error: Playwright is not installed. "
                "Install felix-harness[browser] (uv sync --extra browser)."
            )
        timeout = self._timeout_ms
        try:
            launch_args = await self._pin_args(url)
        except (EgressBlocked, ValueError) as exc:
            return f"browser_error: {exc}"
        try:
            async with async_playwright() as p:
                launcher = getattr(p, self._binding, p.chromium)
                browser = await launcher.launch(headless=True, args=launch_args)
                page = await browser.new_page()
                try:
                    await self._install_egress_guard(page)
                    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    return await self._extract(page, url)
                finally:
                    await browser.close()
        except Exception as exc:
            return f"browser_error: {exc}"

    async def _pin_args(self, url: str) -> list[str]:
        """Chromium launch flags pinning the navigation host to a validated address.

        Without this the check and the load are two independent lookups: the guard resolves
        the model-supplied name, Chromium resolves it again, and a record with a short TTL
        answers them differently. This is the one outbound path where the URL comes from the
        model rather than a manifest, so it is the highest-value rebinding target here.

        Only the navigation host is pinned. Cross-host subresources and redirects keep
        resolving normally and stay covered by the per-request guard, which is advisory —
        denying them outright would break any page that loads assets from a CDN.
        """
        host = (urlparse(url).hostname or "").strip().lower()
        if not host or ipaddress_is_literal(host):
            # A literal needs no rule: Chromium does not resolve it, and `_check_url` has
            # already validated the address itself.
            return []
        if not _SAFE_HOSTNAME.match(host):
            raise ValueError(f"refusing to pin an unexpected hostname: {host!r}")
        addresses = await approved_addresses(host, allow_http=self._allow_http)
        return [f"--host-resolver-rules={_resolver_rule(host, addresses[0])}"]

    async def _extract(self, page: Any, url: str) -> str:
        op = self._op
        if op == "content":
            text = await page.inner_text("body")
            return text[:50_000]
        if op == "links":
            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.href).filter(Boolean)",
            )
            lines = [str(h) for h in (hrefs or [])][:200]
            return "\n".join(lines) or "(no links)"
        if op == "snapshot":
            title = await page.title()
            text = await page.inner_text("body")
            host = urlparse(url).netloc
            return f"# {title}\n\nhost: {host}\nurl: {url}\n\n{text[:20_000]}"
        if op == "json":
            title = await page.title()
            html = await page.content()
            return json.dumps({"url": url, "title": title, "html": html[:15_000]})
        if op == "screenshot":
            png = await page.screenshot(full_page=False)
            if len(png) > _MAX_INLINE:
                return f"[screenshot {len(png)} bytes; too large to inline]"
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        if op == "pdf":
            pdf = await page.pdf()
            if len(pdf) > _MAX_INLINE:
                return f"[pdf {len(pdf)} bytes; too large to inline]"
            return "data:application/pdf;base64," + base64.b64encode(pdf).decode("ascii")
        return f"browser_error: unknown op {op}"


def tools_from_browser_refs(
    refs: list[BrowserToolRef],
    *,
    allow_http: bool = False,
) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        timeout = int(ref.timeout_ms or 15_000)
        executor = _BrowserExecutor(
            op=ref.op,
            timeout_ms=timeout,
            path_prefix=ref.path_prefix,
            allow_http=allow_http,
            binding=ref.binding,
        )
        desc = ref.description or f"Browser {ref.op} via Playwright ({ref.binding or 'chromium'})."
        out.append(
            define_tool_with_executor(
                name=ref.name,
                description=desc,
                args=BrowserUrlArgs,
                executor=executor,
                source="browser",
                fatal=ref.fatal,
            )
        )
    return out


__all__ = ["tools_from_browser_refs"]
