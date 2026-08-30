#!/usr/bin/env python3
"""Verify the Scalar bundle's SRI hash still matches the pinned URL.

`/docs` loads Scalar from a pinned CDN path with an `integrity` attribute. The two
constants must move together: bump the version and forget the hash and the browser
refuses the script, `Scalar` is undefined, and the page renders an empty div — while
the server still answers `200 text/html`, so every health check, smoke test, and unit
test stays green. Only a human loading the page would notice.

    uv run python scripts/check-scalar-sri.py

Needs network. Offline it skips, unless FELIX_REQUIRE_SCALAR_SRI=1 — CI sets that, so
a missing network there is a failure rather than a silent pass.
"""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.error
import urllib.request

from felix_api.docs import SCALAR_JS_SRI, SCALAR_JS_URL

REQUIRED = os.getenv("FELIX_REQUIRE_SCALAR_SRI") == "1"


def _unavailable(reason: str) -> int:
    """Transport failure or a transient CDN status — fatal only where CI says so."""
    if REQUIRED:
        print(f"FAIL  {SCALAR_JS_URL} unreachable: {reason}")
        return 1
    print(f"SKIP  {reason}; set FELIX_REQUIRE_SCALAR_SRI=1 to make this fatal")
    return 0


def main() -> int:
    try:
        with urllib.request.urlopen(SCALAR_JS_URL, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # An HTTP status is an answer, not a missing network. A withdrawn or mistyped
        # version is a 404 — the likeliest spelling of the mistake this script exists
        # to catch — so it must not be reported as "offline" and skipped.
        if exc.code != 429 and exc.code < 500:
            print(f"FAIL  {SCALAR_JS_URL} returned HTTP {exc.code}; the pinned version is wrong")
            return 1
        return _unavailable(f"CDN returned HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return _unavailable(str(exc))

    digest = "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
    if digest != SCALAR_JS_SRI:
        print(
            "FAIL  the pinned Scalar bundle does not match SCALAR_JS_SRI.\n"
            f"      url:      {SCALAR_JS_URL}\n"
            f"      expected: {SCALAR_JS_SRI}\n"
            f"      actual:   {digest}\n"
            "      Update SCALAR_JS_SRI in apps/api/src/felix_api/docs.py to the actual value."
        )
        return 1

    print(f"OK    {SCALAR_JS_URL} matches its integrity hash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
