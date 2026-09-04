"""The gitleaks allowlist must excuse one string, and the CI probe must stay in place.

`.gitleaks.toml` excuses a single false positive that lives in git history. Two earlier
attempts at guarding it were themselves the defect they guarded against:

1. The entry was scoped with `regexes` **and** `paths`. gitleaks ORs the conditions inside
   an allowlist, so the path alone excused every `generic-api-key` finding in that file — a
   real secret added there went undetected.
2. The guard replacing it banned regex metacharacters, on the theory that a literal is
   narrow. It is not: `regexes = ['''key''']` has no metacharacters, passes a metachar ban,
   and silences the rule entirely (measured). gitleaks matches allowlist regexes as
   substrings, so a short literal reaches as far as `.*`. That check also rejected every
   dotted hostname and URL, which pushed the real entry toward a *broader* truncated form.

So this file stops classifying entries and pins the corpus instead: the allowlist set is
one known pair, and any addition or edit goes red, forcing the author through the CI probe.

**This is not the control.** There are at least five suppression channels — allowlist
`regexes`/`paths`/`stopwords`/`commits`/`targets`, `extend.disabledRules`, a `[[rules]]`
block redefining a default rule, and `.gitleaksignore` — and a structural test can only
encode one person's theory about which of them matter. The control is the
"gitleaks still detects a planted secret" step in `.github/workflows/security.yml`, which
tests the property rather than the theory. This file holds the shape between runs and makes
sure that step is not quietly deleted.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"
WORKFLOW = ROOT / ".github/workflows/security.yml"

# Every allowlist entry, as (rule id, regex). Pinned rather than pattern-matched: the
# corpus is one entry, and "is this new entry narrow?" is a question no assertion can
# answer — only the CI probe can.
EXPECTED_ALLOWLISTS = {("generic-api-key", "memoturn-api:3001")}


def _allowlists() -> list[tuple[str, dict]]:
    """Every allowlist, in all four positions gitleaks accepts one."""
    data = tomllib.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    found: list[tuple[str, dict]] = []
    for rule in data.get("rules", []):
        rule_id = str(rule.get("id", "<unnamed>"))
        found.extend((rule_id, entry) for entry in rule.get("allowlists", []))
        if "allowlist" in rule:
            found.append((rule_id, rule["allowlist"]))
    found.extend(("<global>", entry) for entry in data.get("allowlists", []))
    if "allowlist" in data:
        found.append(("<global>", data["allowlist"]))
    return found


def test_the_allowlist_corpus_is_exactly_what_was_reviewed() -> None:
    """Adding or widening an entry must be a deliberate, reviewed act.

    Failing here is not a sign you did something wrong — it means the config changed and
    the new entry needs the docker recipe in `.gitleaks.toml`'s header run against it
    before this set is updated.
    """
    actual = {(rule_id, regex) for rule_id, entry in _allowlists() for regex in (entry.get("regexes") or [])}
    assert actual == EXPECTED_ALLOWLISTS, (
        f"gitleaks allowlists changed: {actual ^ EXPECTED_ALLOWLISTS}. Verify the new entry "
        "does not silence the rule (see the recipe in .gitleaks.toml), then update "
        "EXPECTED_ALLOWLISTS."
    )


def test_an_allowlist_carries_nothing_but_a_regex_and_a_reason() -> None:
    """Trust is an allowlist, applied to the allowlist itself.

    `paths`, `stopwords`, `commits` and `targets` each widen an entry, and `commits` undoes
    the `fetch-depth: 0` the workflow exists to preserve. Naming the permitted keys covers
    all four and anything gitleaks adds later; naming the forbidden ones would not.
    """
    permitted = {"description", "regexes", "regexTarget"}
    for rule_id, entry in _allowlists():
        extra = set(entry) - permitted
        assert not extra, (
            f"{rule_id}: allowlist keys {sorted(extra)} widen the entry beyond its regex. "
            f"Only {sorted(permitted)} are permitted."
        )
        assert entry.get("description", "").strip(), (
            f"{rule_id}: say why, or a later reader cannot judge whether it still applies"
        )


def test_the_default_ruleset_is_extended_and_nothing_is_disabled() -> None:
    """Two bypasses larger than any allowlist.

    Without `useDefault`, declaring a rule id replaces the default rule rather than adding
    to it. And `extend.disabledRules` silences rules outright — measured: with
    `generic-api-key` and `aws-access-token` disabled, a planted secret produced no finding
    while the previous version of this file stayed green.

    `useDefault = true` also closes a third: gitleaks refuses to load a config that sets
    both `extend.path` and `extend.useDefault`, so an over-broad allowlist cannot be hidden
    in an extended file.
    """
    if not CONFIG.exists():
        return
    extend = tomllib.loads(CONFIG.read_text()).get("extend", {})
    assert extend.get("useDefault") is True, "the default ruleset must be extended, not replaced"
    assert not extend.get("disabledRules"), (
        f"extend.disabledRules silences rules wholesale: {extend.get('disabledRules')}"
    )
    assert not extend.get("path"), "an extended config file is a suppression channel nothing here reads"


def test_ci_still_proves_the_scanner_fires() -> None:
    """The actual control. A clean scan is meaningless if the config silenced the rules.

    Without this step, `no leaks found` means either "clean" or "suppressed", and the two
    are indistinguishable — which is exactly how a `paths`-scoped allowlist turned the
    scanner off for a whole file and was reported as verified.
    """
    workflow = WORKFLOW.read_text()
    assert "gitleaks still detects a planted secret" in workflow, (
        "the planted-secret step is gone from security.yml; nothing then proves the Secret scan job can fail"
    )
    assert "openssl rand" in workflow, "the planted value must be random per run, or it can be allowlisted"
