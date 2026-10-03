"""The catalog loader reads SKILL.md frontmatter as YAML, and nothing that loaded before stops.

`felix.skills.loader.parse_skill_md` used to split each frontmatter line on its first colon.
It now goes through `felix.skills.format.parse_skill_md`, which reads real YAML (quoting,
nested `metadata:`, multi-line values), and falls back to the line reader when the YAML does
not parse, since `description: Use it: daily` was valid to the old reader and is a YAML error.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from felix.skills.loader import parse_skill_md
from felix.skills.types import Skill

ROOT = Path(__file__).resolve().parents[2]

_OLD_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_OLD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _old_parse_skill_md(raw: str, *, fallback_name: str, path: str | None = None) -> Skill | None:
    """The line reader as it stood before the switch, frozen here as the reference."""
    text = raw.lstrip("﻿")
    m = _OLD_FRONTMATTER_RE.match(text)
    meta: dict[str, str] = {}
    body = text.strip()
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip().lower()] = val.strip().strip("\"'")
        body = m.group(2).strip()
    name = (meta.get("name") or fallback_name).strip().lower()
    description = (meta.get("description") or "").strip()
    if not description:
        return None
    assert _OLD_NAME_RE.match(name)
    return Skill(
        name=name,
        description=description[:1024],
        body=body,
        path=path,
        version=meta.get("version"),
        metadata={k: v for k, v in meta.items() if k not in {"name", "description"}},
        disable_model_invocation=meta.get("disable-model-invocation", "").lower() in {"true", "1", "yes"},
    )


def _skill_files() -> list[Path]:
    return sorted([*(ROOT / "skills").glob("*/SKILL.md"), *(ROOT / "fixtures" / "skills").glob("*/SKILL.md")])


@pytest.mark.parametrize("skill_md", _skill_files(), ids=lambda p: p.parent.relative_to(ROOT).as_posix())
def test_every_shipped_and_fixture_skill_parses_as_it_did(skill_md: Path) -> None:
    raw = skill_md.read_text(encoding="utf-8")
    new = parse_skill_md(raw, fallback_name=skill_md.parent.name, path=str(skill_md))
    old = _old_parse_skill_md(raw, fallback_name=skill_md.parent.name, path=str(skill_md))
    assert new is not None and old is not None
    # The old reader also kept a nested `metadata:` key itself, as an empty string — the
    # one difference, and only for a skill that has the block.
    old.metadata.pop("metadata", None)
    assert new == old


def test_the_shipped_skills_are_found() -> None:
    """The parametrized case passes vacuously over an empty glob."""
    assert len(list((ROOT / "skills").glob("*/SKILL.md"))) >= 5


def test_a_colon_in_an_unquoted_description_falls_back_to_the_line_reader(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = "---\nname: daily\ndescription: Use it: daily\nversion: 1.0\n---\n\nBody.\n"
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        skill = parse_skill_md(raw, fallback_name="x", path="skills/daily/SKILL.md")
    assert skill is not None
    assert (skill.name, skill.description, skill.version, skill.body) == (
        "daily",
        "Use it: daily",
        "1.0",
        "Body.",
    )
    assert "skills/daily/SKILL.md" in caplog.text
    assert "not spec YAML" in caplog.text


def test_nested_metadata_merges_into_skill_metadata() -> None:
    raw = (
        "---\nname: demo\ndescription: Demo.\nlicense: MIT\nmetadata:\n  Author: me\n  version: 1.2.0\n"
        "  updated: 2026-01-01\n---\nbody"
    )
    skill = parse_skill_md(raw, fallback_name="demo")
    assert skill is not None
    assert skill.metadata == {"license": "MIT", "author": "me", "version": "1.2.0", "updated": "2026-01-01"}
    assert skill.version == "1.2.0"


@pytest.mark.parametrize(
    ("value", "expected"), [("1.10", "1.10"), ("2", "2"), ('"1.0"', "1.0"), ("v3", "v3")]
)
def test_a_version_keeps_its_text(value: str, expected: str) -> None:
    """YAML reads `1.10` as the float 1.1; the version is the text as written."""
    skill = parse_skill_md(f"---\nname: v\ndescription: d\nversion: {value}\n---\nb", fallback_name="v")
    assert skill is not None and skill.version == expected and skill.metadata["version"] == expected


@pytest.mark.parametrize(
    ("value", "hidden"), [("true", True), ('"true"', True), ("yes", True), ("1", True), ("false", False)]
)
def test_disable_model_invocation(value: str, hidden: bool) -> None:
    raw = f"---\nname: h\ndescription: d\ndisable-model-invocation: {value}\n---\nb"
    skill = parse_skill_md(raw, fallback_name="h")
    assert skill is not None and skill.disable_model_invocation is hidden


def test_yaml_quoting_and_multiline_values_are_read_properly() -> None:
    raw = "---\nname: q\ndescription: >\n  Folded across\n  two lines.\nlicense: 'it''s MIT'\n---\nb"
    skill = parse_skill_md(raw, fallback_name="q")
    assert skill is not None
    # The line reader returned ">" as the description and "it''s MIT" as the license.
    assert skill.description == "Folded across two lines."
    assert skill.metadata["license"] == "it's MIT"


@pytest.mark.parametrize(
    "raw",
    [
        "---\nname: m\n---\nbody",
        "---\nname: m\ndescription:\n---\nbody",
        "---\nname: m\ndescription: ''\n---\nbody",
        "# no frontmatter at all",
    ],
)
def test_a_missing_description_skips_the_skill(raw: str) -> None:
    assert parse_skill_md(raw, fallback_name="m") is None


def test_a_bad_name_warns_and_still_loads(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        skill = parse_skill_md("---\nname: Not_Valid\ndescription: d\n---\nb", fallback_name="x")
    assert skill is not None and skill.name == "not_valid"
    assert "invalid" in caplog.text


def test_name_falls_back_and_description_is_truncated() -> None:
    skill = parse_skill_md(f"---\ndescription: {'x' * 2000}\n---\nb", fallback_name="Dir-Name")
    assert skill is not None
    assert skill.name == "dir-name"
    assert len(skill.description) == 1024


@pytest.mark.parametrize(
    "raw",
    [
        "﻿---\nname: e\ndescription: d\n---\n\n body \n",
        "---\r\nname: e\r\ndescription: d\r\n---\r\n\r\n body \r\n",
        "---  \nname: e\ndescription: d\n---\t\nbody",
    ],
    ids=["bom", "crlf", "trailing-fence-whitespace"],
)
def test_encodings_and_fence_variants_still_load(raw: str) -> None:
    skill = parse_skill_md(raw, fallback_name="x")
    assert skill is not None
    assert (skill.name, skill.description, skill.body) == ("e", "d", "body")


def test_unknown_top_level_keys_become_string_metadata() -> None:
    raw = "---\nname: u\ndescription: d\nAllowed-Tools: Bash Read\ntags: [a, b]\n---\nb"
    skill = parse_skill_md(raw, fallback_name="u")
    assert skill is not None
    assert skill.metadata == {"allowed-tools": "Bash Read", "tags": "[a, b]"}
