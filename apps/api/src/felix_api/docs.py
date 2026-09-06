"""Scalar API reference — the `/docs` UI.

FastAPI's built-in `/docs` is Swagger UI. This replaces it with Scalar rendered
from the same `/openapi.json`: the public docs already documented `/docs` as
Scalar (felix-web `guide/rest-api.mdx`, `internals/architecture.mdx`), so the
code was the half that had drifted. The bundle is a pinned CDN script — Swagger
UI was loaded the same way — so nothing is vendored and no dependency is added.
"""

from __future__ import annotations

import html
import json
import secrets

from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse

# Pinned, not `@latest`: an unpinned tag makes the docs page change under a deployment
# that did not change. The integrity hash is what actually enforces that — jsdelivr
# serves a pinned path immutable, but SRI turns "serves" into "or the browser refuses
# it", on an origin that is public in every auth mode. Both move together on an upgrade:
#   curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.67.0/dist/browser/standalone.js"
SCALAR_JS_SRI = "sha384-6c7Vmx+i0yi8gBbltn0x1cavD+zsMGw2xmXXVyacPJLIGBxwaVimW5TW0WiW17Ir"


def _script_safe(payload: object) -> str:
    """JSON for an inline `<script>`, with `</script>` in any string neutralised."""
    return json.dumps(payload).replace("</", "<\\/")


def scalar_html(*, openapi_url: str, title: str, nonce: str, root_path: str = "") -> str:
    config = _script_safe(
        {
            "url": f"{root_path}{openapi_url}",
            # The two settings that are decisions rather than restatements of Scalar's
            # defaults. Curl first because every Felix guide shows curl, so the default
            # snippet matches what a reader has already been copying.
            "layout": "modern",
            "defaultHttpClient": {"targetKey": "shell", "clientKey": "curl"},
        }
    )
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(title)}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head>
  <body>
    <div id="app"></div>
    <script
      src="{SCALAR_JS_URL}"
      integrity="{SCALAR_JS_SRI}"
      crossorigin="anonymous"
      nonce="{nonce}"
    ></script>
    <script nonce="{nonce}">
      // The spec carries no `servers` block — Felix is self-hosted, so the base URL
      // is whatever host served this page. Without it every curl snippet renders as
      // a bare path and is not copy-pasteable.
      Scalar.createApiReference('#app', {{
        ...{config},
        servers: [{{ url: window.location.origin + {_script_safe(root_path)} }}],
      }});
    </script>
  </body>
</html>
"""


def docs_csp(nonce: str) -> str:
    """The docs page's Content-Security-Policy value.

    Two scripts run on the page — the pinned Scalar bundle and the inline config — and
    both carry the per-response nonce, so no other script can. `'strict-dynamic'` lets
    the nonced bundle load its own chunks and drops the host allowlist a nonce would
    otherwise be undercut by: jsdelivr serves any npm package, so naming the origin is
    naming every script an attacker can publish. Scalar injects its own styles inline,
    hence `'unsafe-inline'` for styles only; it fetches the spec from this origin.
    `frame-ancestors 'none'` is the CSP spelling of `X-Frame-Options: DENY`.
    """
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' 'strict-dynamic'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'none'"
    )


def register_docs(app: FastAPI) -> None:
    """Mount the Scalar reference at `/docs`.

    `create_app` passes `docs_url=None` so FastAPI does not claim the path with
    Swagger UI first, and `redoc_url=None` so there is one reference surface: ReDoc
    was HTML with an inline script and a CDN bundle and no CSP, gated like this page
    but not protected like it. The spec path comes from the app rather than a default
    of our own: a second source of that truth is how `/docs` ends up pointing at a 404
    the day one of them moves.

    `root_path` comes from the request for the same reason. Swagger UI resolved the
    spec per request, so mounting the API under a proxy prefix left a precomputed
    `/docs` pointing at a 404 — with the curl snippets missing the prefix too.
    """
    openapi_url = app.openapi_url or "/openapi.json"
    title = f"{app.title} · API reference"

    @app.get("/docs", include_in_schema=False)
    async def scalar_reference(request: Request) -> HTMLResponse:
        # rstrip mirrors FastAPI's own docs wiring. (A trailing-slash root_path does
        # not route at all.)
        root_path = request.scope.get("root_path", "").rstrip("/")
        nonce = secrets.token_urlsafe(16)
        page = scalar_html(openapi_url=openapi_url, title=title, root_path=root_path, nonce=nonce)
        # `private`, not `public`: the body varies on a prefix no shared cache can key
        # on — behind a prefix-stripping proxy every variant is `/docs` at the origin.
        # The browser keys on the public URL, and it is the only cache that matters for
        # a page a human loads by hand.
        return HTMLResponse(
            page,
            headers={"cache-control": "private, max-age=300", "content-security-policy": docs_csp(nonce)},
        )
