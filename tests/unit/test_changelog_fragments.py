"""The changelog assembler, including the failure that made it worth writing.

`scripts/changelog.py` exists because every pull request appended to the top of one block in
`CHANGELOG.md`, so any two open at once conflicted there. What makes that conflict worse than
most is that resolving it is prose — no merge tool helps, and a botched resolution drops an
entry silently rather than failing. Six went that way once.

So the property worth testing is not "it renders markdown". It is that nothing is lost: every
fragment reaches the file, and `[Unreleased]` entries written before this directory existed
are carried rather than replaced.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from tests._scripts import load_script

changelog = load_script("changelog")


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A throwaway changelog and fragment directory, so no test writes the real ones."""
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] — 2026-01-01\n\n### Added\n\n- First.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setattr(changelog, "FRAGMENTS", fragments)
    monkeypatch.setattr(changelog, "CHANGELOG", tmp_path / "CHANGELOG.md")
    return tmp_path


def _fragment(repo: pathlib.Path, name: str, body: str) -> None:
    (repo / "changelog.d" / name).write_text(body, encoding="utf-8")


def test_a_misnamed_fragment_is_refused(repo: pathlib.Path, capsys: Any) -> None:
    """A section nobody recognises would otherwise ship under a heading nobody reads."""
    _fragment(repo, "fixd-typo.md", "**A typo in the section name.**")

    assert changelog.check() == 1
    assert "fixd-typo.md" in capsys.readouterr().err


def test_an_empty_fragment_is_refused(repo: pathlib.Path) -> None:
    _fragment(repo, "fixed-nothing.md", "   \n")

    assert changelog.check() == 1


def test_sections_render_in_keep_a_changelog_order(repo: pathlib.Path) -> None:
    """Not the order the files happen to sort in, which is alphabetical and wrong."""
    _fragment(repo, "fixed-b.md", "**A fix.**")
    _fragment(repo, "added-a.md", "**An addition.**")
    _fragment(repo, "security-c.md", "**A security note.**")

    rendered = changelog.render()

    assert rendered.index("### Added") < rendered.index("### Fixed") < rendered.index("### Security")


def test_a_multi_paragraph_fragment_stays_inside_its_bullet(repo: pathlib.Path) -> None:
    """Written as prose without a leading dash, so continuation lines need the indent.

    Without it the second paragraph renders as a sibling of the list rather than part of the
    entry, which is the kind of thing nobody notices until the release notes are published.
    """
    _fragment(repo, "fixed-wrapped.md", "**Headline.**\n\nA second paragraph.\n")

    rendered = changelog.render()

    assert "- **Headline.**" in rendered
    assert "\n  A second paragraph." in rendered


def test_release_keeps_entries_written_before_this_directory_existed(repo: pathlib.Path) -> None:
    """The whole point is that nothing is lost, so the grandfathered half is asserted too."""
    path = repo / "CHANGELOG.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- Written the old way.\n"
        ),
        encoding="utf-8",
    )
    _fragment(repo, "added-new-way.md", "**Written the new way.**")

    assert changelog.release("1.2.3") == 0

    text = path.read_text(encoding="utf-8")
    assert "Written the old way." in text, "a pre-existing Unreleased entry was dropped"
    assert "Written the new way." in text
    assert "## [1.2.3] — " in text
    assert "- First." in text, "an already-released entry was dropped"
    # A fresh empty Unreleased is left behind for the next cycle.
    assert text.index("## [Unreleased]") < text.index("## [1.2.3]")
    assert "[1.2.3]: https://github.com/felix-run/felix/releases/tag/v1.2.3" in text


def test_release_consumes_the_fragments(repo: pathlib.Path) -> None:
    """Left behind, they would ship again in the next release."""
    _fragment(repo, "added-once.md", "**Only once.**")

    assert changelog.release("1.2.3") == 0

    assert not list((repo / "changelog.d").glob("*.md"))
    assert changelog.render() == ""


def test_release_refuses_a_bad_version(repo: pathlib.Path) -> None:
    _fragment(repo, "added-a.md", "**An addition.**")

    assert changelog.release("1.2") == 1
    assert list((repo / "changelog.d").glob("*.md")), "a refused release still ate the fragments"


def test_release_refuses_when_a_fragment_is_malformed(repo: pathlib.Path) -> None:
    """Otherwise the release is where a typo is discovered, which is the worst moment."""
    _fragment(repo, "added-good.md", "**Fine.**")
    _fragment(repo, "nonsense.md", "**Not fine.**")

    assert changelog.release("1.2.3") == 1
    assert "## [1.2.3]" not in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_readme_is_not_mistaken_for_an_entry(repo: pathlib.Path) -> None:
    """It lives in the directory it documents, which is the natural place to put it."""
    _fragment(repo, "README.md", "How to write one of these.")

    assert changelog.check() == 0
    assert changelog.render() == ""
