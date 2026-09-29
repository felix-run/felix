"""An ordered list that is then cut must be ordered all the way down to a unique key.

The ordering rule, fixed by hand six times — the audit and usage cursors, `list_runs`, `list_jobs`'s
collation, `list_active` twice — and each time found by reading rather than by a gate. A listing
ordered on a key that ties (`created_at`, a score) and then truncated returns *which* tied rows?
Postgres answers "whichever", the memory twin answers "insertion order", and the page boundary
falls in a different place on each: the two arms of one contract disagree, and a paging client
sees a row twice or never.

So: over every module — not `**/store.py`, the glob that let `memory/recall.py` go unexamined
through a survey written for this defect — every SQL `order_by(...)` in a chain with `.limit(...)`,
and every Python sort whose result is sliced, must end on a unique key: a primary-key component of
the model it orders, or, for a Python key, an identifier named like one. A site that cannot must be
`EXEMPT` with the reason ties are harmless there, or `KNOWN_OPEN` — debt, recorded, that this test
refuses to let a fixed site stay listed in, so the list only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "packages" / "harness" / "src" / "felix"

# Sites (`file:function`) whose ordering may tie, and why a tie there is harmless.
EXEMPT = {
    "artifacts.py:expired_artifacts": "retention batch: every row past the cutoff goes; a tie only moves it a batch",
    "attachments.py:expired_attachments": "retention batch: every row past the cutoff goes; a tie only moves it a batch",
    "durability/fibers.py:_claim_due_postgres": "claim batch: a tie only changes which tick claims the fiber",
    "durability/webhooks.py:_claim_due": "claim batch: a tie only changes which sweep delivers",
    "memory/recall.py:_channels_in_memory": "`s[1]` is `_tiebreak(row)` — importance, recency, then the id",
    "documents/store.py:search_documents": "`kv[0]` is the chunk id, unique among the fused hits",
    "documents/store.py:_channels_in_memory": "`kv[0]` is the chunk id; the lexical sort ends on `r['id']`",
    "documents/store.py:list_documents": "one row per `doc_id` (grouped), and both arms end on it",
    "session/strategies.py:render": "a stable sort over seq-ordered events: ties keep seq order",
    "skills/suggest.py:_rank": "ends on the skill's position in the catalogue, unique per skill",
    "tools/decider_retrieval.py:shortlist": "ends on the tool's position in the offered list, unique per tool",
}

# Sites that may tie and matter — debt from `docs/ROADMAP.md`, "More listings whose two arms can
# disagree about order". Fix one and this test tells you to delete its entry.
KNOWN_OPEN: dict[str, str] = {
    # Empty since `list_approvals`, `list_plans` and `consolidate_pools` gained their id
    # tiebreaks. Debt found later goes here, with a line saying what ties — never into EXEMPT.
}

# Files the scan must reach, or it has stopped scanning what it was written for.
MUST_REACH = {"memory/recall.py", "documents/store.py", "session/strategies.py"}


def _unique_names() -> set[str]:
    """Every primary-key column name except the tenant, which never distinguishes rows in a listing."""
    from felix.db.models import Base

    return {c.name for t in Base.metadata.tables.values() for c in t.primary_key.columns} - {"tenant_id"}


def _pk_columns(model_name: str) -> set[str]:
    from felix.db import models

    model = getattr(models, model_name, None)
    table = getattr(model, "__table__", None)
    return {c.name for c in table.primary_key.columns} if table is not None else set()


def _unwrap(expr: ast.expr) -> ast.expr:
    """`X.desc()`, `X.asc()`, `collate(X, 'C')` → `X`."""
    while True:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and not expr.args:
            if expr.func.attr in {"desc", "asc", "nulls_last", "nulls_first"}:
                expr = expr.func.value
                continue
        if isinstance(expr, ast.Call) and getattr(expr.func, "id", None) == "collate" and expr.args:
            expr = expr.args[0]
            continue
        return expr


def _sql_last_key_unique(args: list[ast.expr]) -> bool:
    if len(args) == 1 and isinstance(args[0], ast.Starred):
        inner = args[0].value
        if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "keyset_order":
            args = list(inner.args)
    if not args:
        return False
    last = _unwrap(args[-1])
    return (
        isinstance(last, ast.Attribute)
        and isinstance(last.value, ast.Name)
        and last.attr in _pk_columns(last.value.id)
    )


def _key_last_unique(key: ast.expr | None, unique: set[str]) -> bool:
    if not isinstance(key, ast.Lambda):
        return False
    body = key.body.elts[-1] if isinstance(key.body, ast.Tuple) and key.body.elts else key.body
    if isinstance(body, ast.Subscript) and isinstance(body.slice, ast.Constant):
        name = body.slice.value  # `r["run_id"]`: the memory twins' rows are dicts
    else:
        name = (
            body.attr if isinstance(body, ast.Attribute) else body.id if isinstance(body, ast.Name) else None
        )
    return name in unique


def _sort_key(call: ast.Call) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == "key"), None)


def _is_sorted(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "sorted"


# How many sites of each kind were matched on the last scan — the floors below read these.
MATCHED: dict[str, int] = {"sql": 0, "python": 0}


def _sites() -> dict[str, bool]:
    """`file:function` → whether every ordering-then-cut in it ends on a unique key."""
    unique = _unique_names()
    sites: dict[str, bool] = {}
    MATCHED.update(sql=0, python=0)

    def note(where: str, ok: bool, kind: str = "python") -> None:
        MATCHED[kind] += 1
        sites[where] = sites.get(where, True) and ok

    for path in sorted(HARNESS.rglob("*.py")):
        rel = str(path.relative_to(HARNESS))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            where = f"{rel}:{fn.name}"
            sorted_names: dict[str, ast.expr | None] = {}
            for node in ast.walk(fn):
                # SQL: an `order_by` in a chain that also `limit`s.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "limit"
                ):
                    link: ast.expr = node.func.value
                    while isinstance(link, ast.Call) and isinstance(link.func, ast.Attribute):
                        if link.func.attr == "order_by":
                            note(where, _sql_last_key_unique(list(link.args)), "sql")
                            break
                        link = link.func.value
                # Python: a name bound to a sorted result, or sorted in place.
                if (
                    isinstance(node, ast.Assign)
                    and _is_sorted(node.value)
                    and isinstance(node.targets[0], ast.Name)
                ):
                    sorted_names[node.targets[0].id] = _sort_key(node.value)  # type: ignore[arg-type]
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sort"
                    and isinstance(node.func.value, ast.Name)
                ):
                    sorted_names[node.func.value.id] = _sort_key(node)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
                    continue
                value = node.value
                if _is_sorted(value):
                    note(where, _key_last_unique(_sort_key(value), unique))  # type: ignore[arg-type]
                elif isinstance(value, ast.ListComp | ast.GeneratorExp) and _is_sorted(
                    value.generators[0].iter
                ):
                    note(where, _key_last_unique(_sort_key(value.generators[0].iter), unique))  # type: ignore[arg-type]
                elif isinstance(value, ast.Name) and value.id in sorted_names:
                    note(where, _key_last_unique(sorted_names[value.id], unique))
    return sites


def test_every_ordered_and_cut_listing_ends_on_a_unique_key() -> None:
    sites = _sites()
    ties = sorted(
        where for where, ok in sites.items() if not ok and where not in EXEMPT and where not in KNOWN_OPEN
    )
    assert ties == [], (
        "ordered, then cut, on a key that can tie — end the ordering on a primary-key component "
        f"(`Model.id`, the row's id in a sort key), or say in EXEMPT why a tie is harmless: {ties}"
    )


def test_the_scan_still_reaches_what_it_was_written_for() -> None:
    """A scanner that quietly stops matching passes; one did here, for a whole release."""
    sites = _sites()
    reached = {where.split(":")[0] for where in sites}
    assert reached >= MUST_REACH, f"the scan no longer reaches {sorted(MUST_REACH - reached)}"
    # One floor per kind: with SQL matching broken, the Python sites alone cleared a single
    # floor, and a scanner that loses half its reach while passing is what the floor is for.
    assert MATCHED["sql"] >= 12, f"only {MATCHED['sql']} SQL ordering sites matched; SQL matching broke"
    assert MATCHED["python"] >= 16, f"only {MATCHED['python']} sorted-then-cut sites matched; that half broke"


def test_the_exception_lists_only_hold_sites_that_need_them() -> None:
    """KNOWN_OPEN only shrinks: a fixed site that stays listed would hide its next regression."""
    sites = _sites()
    fixed = sorted(where for where in KNOWN_OPEN if sites.get(where) is True)
    gone = sorted(where for where in (*EXEMPT, *KNOWN_OPEN) if where not in sites)
    assert fixed == [], f"these now end on a unique key — delete them from KNOWN_OPEN: {fixed}"
    assert gone == [], f"these are no longer ordering sites — delete their entries: {gone}"
