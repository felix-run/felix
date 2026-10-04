"""A caller's error answer is written, not forwarded from an exception.

An exception's text is the operator's business: an egress proxy's name, a DNS failure, a
filesystem path, an upstream service's own error. A route that returns `str(exc)` — in an
`HTTPException` detail, a JSON body, or any helper that builds one — hands that to whoever called.
CodeQL reports it as `py/stack-trace-exposure`; #475 shipped six of them in one file before it did.

This scans every route module for the shape: inside `except ... as exc`, the bound exception
reaching a `return` or `raise` — as `str(exc)`, `repr(exc)`, `exc.args`, or interpolated into an
f-string — anywhere but a logger call. The fix is a fixed message chosen by the error's code, built
only from values the route already validated, with the detail logged (`routes/repos.py`).

`KNOWN_OPEN` holds the sites that predate this check. It only shrinks: a site fixed is removed, and
a new one is never added here. Being listed is not a finding that a site is safe; it is a finding
that nobody has looked yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "felix_api" / "routes"

# (route module, function) sites returning exception text that predate this check. Most read as
# messages about the caller's own input (a manifest that does not validate, an upload refused) —
# which is what each must be shown to be, one by one, before it leaves this list.
KNOWN_OPEN = {
    ("documents.py", "ingest_document"),
    ("files.py", "upload_file"),
    ("manifests.py", "set_canary"),
    ("manifests.py", "upsert_manifest"),
    ("openai_compat.py", "chat_completions"),
}

_LOGGER_NAMES = {"logger", "log", "logging"}


def _is_logger_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    while isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in _LOGGER_NAMES:
            return True
        func = func.value
    return False


def _mentions(node: ast.AST, name: str) -> bool:
    """Whether `node` turns the exception into text: str()/repr()/format()/.args/an f-string."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in {"str", "repr", "format"}
        ):
            if any(isinstance(a, ast.Name) and a.id == name for a in sub.args):
                return True
        if isinstance(sub, ast.FormattedValue) and isinstance(sub.value, ast.Name) and sub.value.id == name:
            return True
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "args"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == name
        ):
            return True
    return False


def _leaks(statement: ast.stmt, name: str) -> bool:
    """A return/raise whose value carries the exception's text, ignoring logger calls inside it."""
    if isinstance(statement, ast.Return) and statement.value is not None:
        value: ast.AST = statement.value
    elif isinstance(statement, ast.Raise) and statement.exc is not None:
        value = statement.exc
    else:
        return False
    if _is_logger_call(value):
        return False
    return _mentions(value, name)


def _offenders() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for handler in ast.walk(func):
                if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                    continue
                for statement in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                    if isinstance(statement, ast.stmt) and _leaks(statement, handler.name):
                        found.add((path.name, func.name))
    return found


def test_no_route_answers_with_an_exception_s_text() -> None:
    new = _offenders() - KNOWN_OPEN
    assert not new, (
        "these routes put an exception's text in a caller's answer — return a fixed message chosen by "
        f"the error's code, log the detail (see routes/repos.py): {sorted(new)}"
    )


def test_known_open_only_shrinks() -> None:
    """A grandfathered site that no longer leaks must leave the list, or the list stops meaning anything."""
    fixed = KNOWN_OPEN - _offenders()
    assert not fixed, f"fixed — remove from KNOWN_OPEN: {sorted(fixed)}"


def test_the_scan_finds_the_shape_it_exists_for() -> None:
    """Mutation check: the scan must catch a leak, or a green run proves nothing."""
    code = (
        "async def route():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        logger.warning('x %s', exc)\n"
        "        return _refusal(502, 'code', f'failed: {exc}')\n"
    )
    handler = next(n for n in ast.walk(ast.parse(code)) if isinstance(n, ast.ExceptHandler))
    assert any(_leaks(s, "exc") for s in handler.body)
    logged_only = (
        "try:\n    pass\nexcept ValueError as exc:\n    logger.warning('x %s', str(exc))\n    return None\n"
    )
    handler = next(n for n in ast.walk(ast.parse(logged_only)) if isinstance(n, ast.ExceptHandler))
    assert not any(_leaks(s, "exc") for s in handler.body)
