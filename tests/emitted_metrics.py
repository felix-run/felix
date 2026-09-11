"""Every metric name the source emits, derived from the tree once per run.

The catalog test, the alert-rule test and any dashboard test all need the same set; each
walking every `.py` under `packages/` and `apps/` with `ast` on its own would triple the
cost and let the recorder table drift between copies.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "packages", ROOT / "apps")

# recorder name -> index of the positional metric-name argument
RECORDERS = {"record_counter": 0, "record_counter_detached": 1, "record_histogram": 0}
# `timed_span` records a histogram (and optionally a counter) on the caller's behalf, so the
# name lives in a keyword at the call site rather than in a positional arg to a recorder.
# A scan that only knew the recorders reported every one of those as undocumented.
RECORDER_KEYWORDS = {"timed_span": ("metric", "counter")}


@cache
def emitted_metric_names() -> frozenset[str]:
    """Every metric name passed as a literal to a recorder, across the whole tree."""
    found: set[str] = set()
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover - the tree parses or lint already failed
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                index = RECORDERS.get(node.func.id)
                if index is not None and len(node.args) > index:
                    arg = node.args[index]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
                for name in RECORDER_KEYWORDS.get(node.func.id, ()):
                    for kw in node.keywords:
                        if kw.arg == name and isinstance(kw.value, ast.Constant):
                            if isinstance(kw.value.value, str):
                                found.add(kw.value.value)
    return frozenset(found)
