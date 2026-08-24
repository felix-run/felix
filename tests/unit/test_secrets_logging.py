"""`felix.secrets` may log the name of a secret. It may never log the value.

CodeQL reports four `py/clear-text-logging-sensitive-data` errors here, and all four
are false positives on the axis they name: every logging call in the module passes a
key *name*, and `tasks.py` passes a backend name and a count. Logging the key rather
than the value is the recommended practice, not a defect.

Dismissing an alert is only honest if the property it doubts is actually guaranteed, so
the property is enforced here rather than asserted in a dismissal comment. The module's
logging calls are enumerated from the source, and anything that could carry a resolved
value is refused by name. A future edit that logs `val` fails this test, which is the
outcome the alert was reaching for.

The adjacent real defect — the rejection path logged a caller-chosen string unescaped,
so a newline in it forged a log entry right beside genuine "rejected" records — is
covered by the injection test at the bottom.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SECRETS = Path(__file__).resolve().parents[2] / "packages/harness/src/felix/secrets.py"

# Names that hold, or can hold, a resolved secret value. Derived by reading the module;
# if one is renamed the enumeration below still catches the call, because the check is
# "every argument is on the allow-list", not "no argument is on the deny-list".
VALUE_BEARING = {"val", "found", "current", "ordered", "parsed", "resp", "_resolved_secret_values"}

# What a logging call in this module is permitted to interpolate.
ALLOWED_ARGS = {"name", "attr", "backend", "path"}


def _logging_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "logger":
            calls.append(node)
    return calls


def _identifiers(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_the_module_still_logs_something() -> None:
    """Guards the guard: an enumeration over nothing passes every assertion below."""
    calls = _logging_calls(ast.parse(SECRETS.read_text()))
    assert len(calls) >= 4, f"expected the module's logging calls to be found, saw {len(calls)}"


def test_no_logging_call_can_reach_a_secret_value() -> None:
    tree = ast.parse(SECRETS.read_text())
    for call in _logging_calls(tree):
        for arg in call.args[1:]:  # args[0] is the format string
            names = _identifiers(arg)
            leaked = names & VALUE_BEARING
            assert not leaked, (
                f"secrets.py:{call.lineno} logs {sorted(leaked)}, which can hold a resolved "
                f"secret value. Log the name, never the value."
            )
            stray = names - ALLOWED_ARGS - {"loggable"}
            assert not stray, (
                f"secrets.py:{call.lineno} logs {sorted(stray)}, which is not on the allow-list "
                f"{sorted(ALLOWED_ARGS)}. Widen the list deliberately if the value is safe."
            )


def test_every_caller_supplied_name_is_escaped_before_logging() -> None:
    """The names reaching these lines are chosen by whoever asked for the secret.

    On the rejection path that is a value the loader just refused, so it is hostile by
    construction — and the entry a newline would forge sits directly beside genuine
    rejection records, which is the worst place to be able to write one.
    """
    tree = ast.parse(SECRETS.read_text())
    for call in _logging_calls(tree):
        for arg in call.args[1:]:
            if "name" not in _identifiers(arg):
                continue
            assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "loggable", (
                f"secrets.py:{call.lineno} logs a caller-supplied name without `loggable`"
            )


@pytest.mark.asyncio
async def test_a_rejected_path_cannot_forge_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end, through the real loader."""
    import logging

    from felix.secrets import FileSecrets

    forged = "../etc/shadow\nWARNING:felix.secrets:file_secrets_accept path=/root/.ssh/id_rsa"
    with caplog.at_level(logging.WARNING, logger="felix.secrets"):
        assert await FileSecrets(tmp_path).get(forged) is None

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1, f"one rejection became {len(messages)} entries: {messages}"
    assert "\n" not in messages[0], f"a newline reached the log: {messages[0]!r}"
    # The attempt stays visible — that is the point of logging a rejection at all.
    assert "etc/shadow" in messages[0]
