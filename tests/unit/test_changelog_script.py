"""`scripts/changelog.py`: the changelog written from pull-request descriptions at release time."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cl() -> Any:
    spec = importlib.util.spec_from_file_location("changelog_script", ROOT / "scripts" / "changelog.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["changelog_script"] = module
    spec.loader.exec_module(module)
    return module


BODY = """## Summary

- did things

## Changelog

<!-- a template comment the author left in -->

### Fixed

- **A thing was broken.** It is not now,
  and this line continues the entry.

### Added

- **A new thing.** It exists.

## Tests

- not part of the changelog
"""


def test_a_description_yields_its_entries_and_nothing_past_the_section(cl: Any) -> None:
    entries = cl.parse(BODY)
    assert [(e.category, e.text.splitlines()[0]) for e in entries] == [
        ("Fixed", "- **A thing was broken.** It is not now,"),
        ("Added", "- **A new thing.** It exists."),
    ]
    assert "continues the entry" in entries[0].text
    assert all("not part of the changelog" not in e.text for e in entries)


@pytest.mark.parametrize("none", ["none: test-only refactor", "None — docs typo", "none - CI only"])
def test_none_with_a_reason_is_no_entries(cl: Any, none: str) -> None:
    assert cl.parse(f"## Changelog\n\n{none}\n") == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("## Summary\n\n- x\n", "no `## Changelog` section"),
        ("## Changelog\n\n<!-- only a comment -->\n\n## Tests\n", "is empty"),
        ("## Changelog\n\nnone\n", "needs a reason"),
        ("## Changelog\n\n### Improved\n\n- x\n", "is not one of"),
        ("## Changelog\n\n- an entry with no heading\n", "before any"),
        ("## Changelog\n\n### Fixed\n\nprose, not an entry\n", "text outside"),
        ("## Changelog\n\n### Fixed\n\n", "no `- ` entries"),
    ],
    ids=["missing", "empty", "bare-none", "unknown-heading", "no-heading", "prose", "headings-only"],
)
def test_an_unusable_section_says_why(cl: Any, body: str, reason: str) -> None:
    with pytest.raises(cl.ChangelogError, match=reason):
        cl.parse(body)


def test_render_orders_categories_the_keep_a_changelog_way(cl: Any) -> None:
    entries = [cl.Entry("Security", "- s"), cl.Entry("Added", "- a"), cl.Entry("Fixed", "- f")]
    out = cl.render("1.2.3", "2026-10-03", entries)
    assert out.splitlines()[0] == "## [1.2.3] — 2026-10-03"
    assert [line for line in out.splitlines() if line.startswith("### ")] == [
        "### Added",
        "### Fixed",
        "### Security",
    ]


TEXT = """# Changelog

## [Unreleased]

### Fixed

- **Written by hand.** Before the switch.

## [0.6.0] — 2026-10-03

### Added

- old

[0.6.0]: https://github.com/felix-run/felix/releases/tag/v0.6.0
"""


def test_cut_closes_unreleased_keeping_hand_written_entries_first(cl: Any) -> None:
    collected = [cl.Entry("Fixed", "- **From a PR.** (#9)"), cl.Entry("Added", "- **New.** (#8)")]
    out = cl.cut(TEXT, "0.7.0", "2026-10-04", collected)
    head, released = out.split("## [0.7.0] — 2026-10-04", 1)
    assert head.rstrip().endswith("## [Unreleased]"), "a fresh, empty Unreleased above the release"
    section = released.split("## [0.6.0]", 1)[0]
    assert section.index("### Added") < section.index("### Fixed")
    assert section.index("Written by hand") < section.index("From a PR")
    assert out.rstrip().endswith("[0.7.0]: https://github.com/felix-run/felix/releases/tag/v0.7.0")
    assert "- old" in out, "earlier releases are untouched"


def test_cut_refuses_a_version_already_written_and_an_empty_release(cl: Any) -> None:
    with pytest.raises(cl.ChangelogError, match="already has a section"):
        cl.cut(TEXT, "0.6.0", "2026-10-04", [])
    empty = TEXT.replace("### Fixed\n\n- **Written by hand.** Before the switch.\n\n", "")
    with pytest.raises(cl.ChangelogError, match="nothing to release"):
        cl.cut(empty, "0.7.0", "2026-10-04", [])


def test_the_current_unreleased_section_carries_over(cl: Any) -> None:
    """Entries written by hand under [Unreleased] must parse, or the next cut fails.

    Not that there are any: a cut leaves the section empty, so asserting it held entries
    failed the very release (0.6.1) that consumed the ones written before the switch.
    """
    text = (ROOT / "CHANGELOG.md").read_text()
    _, _, entries = cl._unreleased(text)
    assert all(e.category in cl.CATEGORIES for e in entries)


def test_merged_numbers_reads_merge_and_squash_subjects_oldest_first(
    cl: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = "\n".join(
        [
            "Merge pull request #12 from felix-run/b",
            "Fix a thing (#11)",
            "A direct commit with no PR",
            "Merge pull request #10 from felix-run/a",
        ]
    )
    monkeypatch.setattr(cl, "_run", lambda *argv: log)
    assert cl.merged_numbers("v0.6.0") == [10, 11, 12]


def test_collect_cites_each_entry_and_reports_unusable_descriptions(
    cl: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = {10: BODY, 11: "## Summary\n\nno section\n"}
    monkeypatch.setattr(cl, "merged_numbers", lambda since: [10, 11])
    monkeypatch.setattr(cl, "description", lambda n: bodies[n])
    entries, problems = cl.collect("v0.6.0")
    assert entries[0].text.endswith("continues the entry. (#10)")
    assert problems == ["#11: no `## Changelog` section; add one, or `none: <reason>` under it"]


def test_check_command_exit_codes(cl: Any, tmp_path: pathlib.Path) -> None:
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text(BODY)
    bad.write_text("## Summary\n")
    assert cl.main(["check", "--body-file", str(good)]) == 0
    assert cl.main(["check", "--body-file", str(bad)]) == 1


TRAILER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def test_a_changelog_written_last_ends_at_the_attribution_trailer(cl: Any) -> None:
    """#462 failed its check: the trailer under a final `## Changelog` read as a stray line."""
    body = f"Summary.\n\n## Changelog\n\n### Fixed\n\n- **A fix.** Why it matters.\n\n{TRAILER}\n"
    assert cl.parse(body) == [cl.Entry("Fixed", "- **A fix.** Why it matters.")]


def test_other_text_after_the_entries_still_fails(cl: Any) -> None:
    body = "## Changelog\n\n### Fixed\n\n- **A fix.**\n\nA stray paragraph.\n"
    with pytest.raises(cl.ChangelogError, match="text outside"):
        cl.parse(body)
