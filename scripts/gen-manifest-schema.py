#!/usr/bin/env python3
"""Generate schemas/manifest.schema.json from the pydantic manifest models.

The bundled manifests carry a `# yaml-language-server: $schema=...` header, so
editors resolve this file for completion and validation. It is generated, never
hand-edited: `tests/unit/test_invariants.py` fails when it drifts from
`felix.manifests.schema.Manifest`.

    python scripts/gen-manifest-schema.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "schemas" / "manifest.schema.json"

SCHEMA_ID = "https://felix.run/schemas/manifest.schema.json"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def build() -> dict:
    from felix.manifests.schema import Manifest

    schema = Manifest.model_json_schema()
    schema["title"] = "Felix agent manifest (apiVersion: felix/v1, kind: Agent)"
    return {"$schema": SCHEMA_DIALECT, "$id": SCHEMA_ID, **schema}


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv[1:]:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is stale — run: make schema", file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
