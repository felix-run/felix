"""Every store row serializer spells its keys out, in a `return {...}` literal.

The one `_<row>_dict(row)` per table convention is a contract with felix-run/web: its payload
recorder reads each serializer's literal keys to guard the client's row types, because the OpenAPI
spec documents every JSON response as a bare object. A serializer that builds its dict in a helper
and adds a key after — `eval/store.py:_run_dict` did, when run `stats` were added — becomes
unreadable, and the client guard pointed at it goes on passing while checking nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "packages" / "harness" / "src" / "felix"

# Serializers that are not a row's shape, with why.
NOT_A_ROW = {
    "usage/store.py:_summary_totals_dict": "an aggregate summed over _SUMMED_COLUMNS, not a stored row",
}


def _serializers() -> dict[str, ast.FunctionDef]:
    found = {}
    for path in sorted(HARNESS.rglob("store.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_") and node.name.endswith("dict"):
                found[f"{path.relative_to(HARNESS)}:{node.name}"] = node
    return found


def _returns_a_literal(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Dict)
        and any(isinstance(k, ast.Constant) for k in node.value.keys)
        for node in ast.walk(fn)
    )


def test_every_row_serializer_returns_its_keys_literally() -> None:
    serializers = _serializers()
    assert len(serializers) >= 10, "the scan stopped finding the serializers it exists for"
    unreadable = [
        name for name, fn in serializers.items() if name not in NOT_A_ROW and not _returns_a_literal(fn)
    ]
    assert unreadable == [], f"serializers a client cannot read the keys of: {unreadable}"
    stale = [name for name in NOT_A_ROW if name not in serializers]
    assert stale == [], f"NOT_A_ROW entries that no longer exist: {stale}"
