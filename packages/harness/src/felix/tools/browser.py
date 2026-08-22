"""Browser tools from ``spec.browser_tools`` — Playwright extra, SSRF-guarded."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import BrowserToolRef
from felix.security.ssrf import assert_safe_outbound_url
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

    def _check_url(self, url: str) -> None:
        assert_safe_outbound_url(url, allow_http=self._allow_http)
        if self._path_prefix and not url.startswith(self._path_prefix):
            raise ValueError(f"url must start with {self._path_prefix!r}")

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        url = str(args.get("url") or "")
        try:
            self._check_url(url)
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
            async with async_playwright() as p:
                launcher = getattr(p, self._binding, p.chromium)
                browser = await launcher.launch(headless=True)
                page = await browser.new_page()
                try:
                    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    return await self._extract(page, url)
                finally:
                    await browser.close()
        except Exception as exc:
            return f"browser_error: {exc}"

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
