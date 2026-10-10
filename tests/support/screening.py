"""Governed manifests, PII text and audit read-back shared by the e2e screening tests."""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix.manifests.schema import Manifest

EMAIL = "alice@example.com"
PII = f"Reach me at {EMAIL} any time."

DECIDER_ENV = {
    "FELIX_DECISION_ROUTES": json.dumps({"e2e-decider": {"provider": "scripted", "model": "jev-latest"}})
}


def _manifest(name: str, spec: dict[str, Any]) -> Manifest:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


def governed_manifest(name: str, **spec: Any) -> Manifest:
    """A minimal governed manifest with the calculator. Anonymous is allowed because the schema
    default is not, and these tests are about what happens *after* the door — a caller with no
    scopes at all, which is what makes a policy denial a real refusal rather than a 401."""
    base = {"pattern": "react", "tools": ["calculator"], "auth": {"inbound": {"allow_anonymous": True}}}
    return _manifest(name, base | spec)


def judged_manifest(**judge: Any) -> Manifest:
    """`e2e-judged`: one on-topic judge, answered by the scripted decider on `DECIDER_ENV`."""
    rule = {"name": "on-topic", "criteria": "is about arithmetic", "threshold": 0.5, "decider": True, **judge}
    return _manifest(
        "e2e-judged",
        {
            "pattern": "react",
            "tools": ["calculator"],
            "auth": {"inbound": {"allow_anonymous": True}},
            "decider": {"id": "e2e-decider"},
            "guardrails": {"judges": [rule]},
        },
    )


async def audit_rows(settings: Any) -> list[tuple[str, str, str]]:
    """Flush and read back `(event_type, status, tool)` — the write path, not the buffer.

    The tool name is part of the tuple because `("tool_call", "ok")` alone is satisfied by an
    audit row for any tool at all.
    """
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    await flush_all(settings)
    rows, _ = await audit_store.query(settings, "default", limit=200)
    return [
        (row["event_type"], row.get("status") or "", (row.get("payload_json") or {}).get("tool") or "")
        for row in rows
    ]
