"""The gitleaks allowlist must excuse a string, never a file.

`.gitleaks.toml` exists to excuse one false positive that is already in git history. The
first version scoped the entry with a `regexes` **and** a `paths` entry, which reads as
narrowing and is the opposite: gitleaks ORs the conditions inside an allowlist, so the path
alone excused every `generic-api-key` finding anywhere in that file. A real secret added
there went undetected. `matchCondition = "AND"` is documented for exactly this and was not
honoured, so the only reliable scoping is a literal regex and nothing else.

This is structural rather than a run of gitleaks, because CI's Secret scan job is a
container this suite cannot start. It cannot prove the scanner still fires — only that the
config keeps the shape that made it fire. The scanner itself is the control; this guards the
one way we know to blunt it silently.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[2] / ".gitleaks.toml"


def _allowlists() -> list[tuple[str, dict]]:
    if not CONFIG.exists():
        return []
    data = tomllib.loads(CONFIG.read_text())
    found: list[tuple[str, dict]] = []
    for rule in data.get("rules", []):
        rule_id = str(rule.get("id", "<unnamed>"))
        for allowlist in rule.get("allowlists", []):
            found.append((rule_id, allowlist))
        if "allowlist" in rule:
            found.append((rule_id, rule["allowlist"]))
    for allowlist in data.get("allowlists", []):
        found.append(("<global>", allowlist))
    if "allowlist" in data:
        found.append(("<global>", data["allowlist"]))
    return found


def test_the_config_still_extends_the_default_ruleset() -> None:
    """Without this, declaring a rule id replaces the default rule instead of adding to it."""
    if not CONFIG.exists():
        pytest.skip("no .gitleaks.toml; the default ruleset applies")
    data = tomllib.loads(CONFIG.read_text())
    assert data.get("extend", {}).get("useDefault") is True


def test_no_allowlist_is_scoped_by_path() -> None:
    """A `paths` entry excuses the whole file, because the conditions are ORed.

    Measured: with a path entry, a random `aws_secret_access_key` appended to the named file
    produced no finding. Without it, the same secret is found. Scope with a literal regex.
    """
    offenders = [
        f"{rule_id}: paths={allowlist['paths']}"
        for rule_id, allowlist in _allowlists()
        if allowlist.get("paths")
    ]
    assert not offenders, (
        f"path-scoped gitleaks allowlists disable the rule for the entire file: {offenders}. "
        "Scope with `regexes` alone — see the comment in .gitleaks.toml."
    )


def test_every_allowlist_has_a_literal_regex_and_a_reason() -> None:
    """A regex broad enough to match a real credential is the other way to blunt this."""
    for rule_id, allowlist in _allowlists():
        regexes = allowlist.get("regexes") or []
        assert regexes, f"{rule_id}: an allowlist with no regex excuses everything the rule finds"
        assert allowlist.get("description", "").strip(), (
            f"{rule_id}: say why, or the next reader cannot judge whether it still applies"
        )
        for regex in regexes:
            assert not any(c in regex for c in ".*+?[](){}|^$\\"), (
                f"{rule_id}: {regex!r} is not a literal — a pattern can match a real secret. "
                "Excuse the exact string."
            )
