"""The metric catalog must describe what the code actually emits.

`docs/ROADMAP.md` called this out: "no metric catalog anywhere, so an operator cannot know
what to graph without grepping call sites". A catalog that drifts is worse than none —
it tells an operator a series exists when it does not, and hides the ones that do.

So the table in `docs/OBSERVABILITY.md` is re-derived from the source here rather than
trusted. Adding a `record_counter("felix_...")` without a row fails this test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/OBSERVABILITY.md"
SOURCE_ROOTS = (ROOT / "packages", ROOT / "apps")

RECORDERS = {"record_counter": 0, "record_counter_detached": 1, "record_histogram": 0}


def _emitted_metric_names() -> set[str]:
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
                if index is None:
                    continue
                if len(node.args) > index:
                    arg = node.args[index]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def _documented_metric_names() -> set[str]:
    # Table cells of the form `| \`felix_x\` | ...`
    return set(re.findall(r"^\|\s*`(felix_[a-z_]+)`\s*\|", DOC.read_text(), re.MULTILINE))


def test_every_emitted_metric_is_documented() -> None:
    undocumented = sorted(_emitted_metric_names() - _documented_metric_names())
    assert not undocumented, (
        f"emitted but missing from docs/OBSERVABILITY.md: {undocumented}. "
        "An operator cannot graph what is not in the catalog."
    )


def test_the_catalog_documents_nothing_fictional() -> None:
    """The claim that rots fastest is that something exists."""
    phantom = sorted(_documented_metric_names() - _emitted_metric_names())
    assert not phantom, f"documented but never emitted: {phantom}"


def test_the_catalog_records_why_metrics_is_authenticated() -> None:
    """A future reader who does not know this will make `/metrics` public to fix a scrape."""
    text = DOC.read_text()
    assert "requires authentication" in text
    assert "manifest ids" in text
