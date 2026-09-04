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

**Two controls are needed and neither subsumes the other**, which an earlier version of
this docstring got backwards:

* The CI probe in `.github/workflows/security.yml` plants secrets beside this config and
  requires a finding. It catches anything that silences a rule outright — `disabledRules`,
  a redefined rule, a global allowlist — including channels nobody has thought of.
* It cannot catch **file-scoped** suppression. The probe directory is not this repo, so a
  rule allowlist carrying `paths`, `stopwords` or `commits` leaves the repo scan silenced
  while the probe still passes. Measured with the original defect's exact shape: a real
  committed secret excused, probe green. The permitted-keys assertion below is what catches
  that class — the class that actually occurred, twice.
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
# (rule id, regex, regexTarget). `regexTarget` is pinned because it is the amplifier:
# with the default target the same regex is inert, and with `match` or `line` it silences
# the rule. Measured for `regexes = ['''key''']` — inert on `secret`, silencing on the
# other two. The existing entry needs `line`, because the part gitleaks treats as the
# secret is the port fragment rather than the hostname.
EXPECTED_ALLOWLISTS = {("generic-api-key", "memoturn-api:3001", "line")}


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
    actual = {
        (rule_id, regex, entry.get("regexTarget", "secret"))
        for rule_id, entry in _allowlists()
        for regex in (entry.get("regexes") or [])
    }
    assert actual == EXPECTED_ALLOWLISTS, (
        f"gitleaks allowlists changed: {actual ^ EXPECTED_ALLOWLISTS}.\n"
        "Before updating this set: the CI probe CANNOT tell you whether a new entry is safe "
        "if it carries `paths`, `stopwords` or `commits` — those suppress the repo scan "
        "while the probe still passes. `test_an_allowlist_carries_nothing_but_a_regex_and_"
        "a_reason` is the check that catches them. Use the probe recipe in .gitleaks.toml "
        "for rule-level silencing, and keep the entry to a regex and a target."
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


def _secrets_job_steps() -> list[dict]:
    """The `secrets` job's steps, parsed.

    Parsed rather than substring-matched: the previous version searched the raw file, so
    commenting a step out passed it — and commenting out is how a step actually gets
    disabled.
    """
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text())
    return [step for step in workflow["jobs"]["secrets"]["steps"] if step]


def test_the_repository_scan_is_still_wired() -> None:
    """The thing the probe exists to validate, which nothing here used to guard.

    Deleting the scan step left the whole suite green: no secret scanning at all, and a
    docstring asserting the control was intact.
    """
    steps = _secrets_job_steps()
    assert [s for s in steps if "git /repo" in str(s.get("run", ""))], (
        "the gitleaks repository scan is gone from security.yml"
    )
    checkout = next(s for s in steps if "checkout" in str(s.get("uses", "")))
    assert str(checkout.get("with", {}).get("fetch-depth")) == "0", (
        "the scan must see full history — a secret removed in a later commit still leaked, "
        "and it is what an allowlist `commits` entry would otherwise undo"
    )


def test_ci_still_plants_a_secret_and_requires_a_finding() -> None:
    """The probe. Catches rule-level suppression; see the module docstring for its limits."""
    probes = [
        s
        for s in _secrets_job_steps()
        if "planted" in str(s.get("name", "")) or "report.json" in str(s.get("run", ""))
    ]
    assert probes, (
        "the planted-secret step is gone from security.yml; nothing then proves the Secret "
        "scan job is capable of failing"
    )
    run = str(probes[0].get("run", ""))
    assert "generic-api-key" in run, "the probe must assert which rule fired, not just an exit code"
    # Scoped to the copy itself. A blanket ban on the string would also forbid explaining
    # in a comment why it must not be there, which is the more useful of the two.
    copies = [line for line in run.splitlines() if line.strip().startswith("cp .gitleaks.toml")]
    assert copies, "the probe must scan against this repo's config, not stock gitleaks"
    assert all("|| true" not in line for line in copies), (
        "`cp .gitleaks.toml ... || true` lets the probe pass with no config at all — it "
        "then tests stock gitleaks and proves nothing about this repo"
    )


def test_no_gitleaksignore_suppresses_findings_by_fingerprint() -> None:
    """The fifth channel, and the only one neither control can see.

    gitleaks reads `.gitleaksignore` automatically and drops findings whose fingerprint it
    lists. The probe cannot detect it — fingerprints are per-finding, so a freshly planted
    secret is never among them. Measured end to end: a real committed secret plus its
    fingerprint in `.gitleaksignore` makes the repo scan exit 0 while the probe passes.

    The repo has never had one. If a genuine need arises, pin its contents here the way
    `EXPECTED_ALLOWLISTS` pins the allowlist corpus.
    """
    ignore = ROOT / ".gitleaksignore"
    assert not ignore.exists(), (
        f"{ignore.name} suppresses findings by fingerprint and is invisible to both the "
        "unit test and the CI probe. Pin its contents here if it is genuinely needed."
    )
