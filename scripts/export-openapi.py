#!/usr/bin/env python3
"""Write the API's OpenAPI document to a file, built from this checkout.

The release workflow attaches the output to each GitHub release as `openapi.json`, and
docs.felix.run renders the one for the version `api.felix.run/health` reports. The API's
own `/docs` sits behind the credential, so this is the public copy of the reference, and it
has to describe what the release serves.

FastAPI builds the spec without a database, so the settings are in-memory and explicit:
`_env_file=None` keeps a repo `.env` (a real Postgres, real vendor keys) out of it. Auth
mode does not change the document. `manifest_source` does: under `bundled` the manifest
write verbs are never mounted, so the export keeps the default `store` and documents the
full surface.

    uv run python scripts/export-openapi.py openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from felix.config import Settings
from felix_api.app import create_app


def build_spec() -> dict:
    settings = Settings(
        _env_file=None,
        database_url="memory://export-openapi",
        object_store="memory",
        environment="development",
        auth_mode="none",
        host="127.0.0.1",
        allow_insecure=True,
        redis_url="",
    )
    return create_app(settings=settings).openapi()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: export-openapi.py <out.json>", file=sys.stderr)
        return 2
    spec = build_spec()
    Path(argv[1]).write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"wrote {argv[1]}: OpenAPI {spec['openapi']}, {len(spec['paths'])} paths, v{spec['info']['version']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
