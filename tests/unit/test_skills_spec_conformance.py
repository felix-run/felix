"""The catalog runtime against agentskills.io's specification and client-integration guide.

Discovery reads `<dir>/SKILL.md` packages and nothing else; a skill the catalog hides (disabled for
the model, or a declared ref nothing answered) is hidden from the skill tools as well; the catalog
block is ordered and says where a skill's relative paths resolve; cosmetic problems warn rather
than drop a skill; and `felix skills validate` is the strict check an author runs before publishing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from felix.session.store import InMemorySessionStore
from felix.session.tree import annotate_and_append
from felix.session.types import chat_message_to_event
from felix.skills import discovery, loader
from felix.skills.format import MAX_BUNDLE_FILES, split_frontmatter, validate_skill_bundle
from felix.skills.tools import MAX_ACTIVATION_CHARS, make_skill_tools
from felix.skills.types import Skill, SkillCatalog
from felix.tools.types import Tool, ToolInvocationCtx, tool_input_schema, tool_output_content
from felix_ai.types import ChatMessage
from felix_cli.main import app as cli
from typer.testing import CliRunner

from tests.support.skill_threads import call_in_thread


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _skill_md(name: str, description: str = "Does a thing. Use when asked.", extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\nBody of {name}.\n"


def _tools(catalog: SkillCatalog) -> dict[str, Tool]:
    tools = make_skill_tools(catalog, session_store=None, tenant_id="t", manifest_id="m")
    return {t.name: t for t in tools}


async def _call(tool: Tool, args: dict[str, Any]) -> dict[str, Any] | list[Any]:
    out = await tool.executor.execute(args, ToolInvocationCtx(thread_id="t:1", tool_call_id="c1"))
    return json.loads(tool_output_content(out))


# -- discovery --------------------------------------------------------------------------------


def test_only_skill_directories_are_skills(tmp_path: Path) -> None:
    """A loose `notes.md` at the root was loaded as a skill named `notes`; the spec's unit is a
    directory holding `SKILL.md`."""
    _write(tmp_path, "real/SKILL.md", _skill_md("real"))
    _write(tmp_path, "notes.md", _skill_md("notes"))
    assert loader.load_skills_from_dir(tmp_path).names() == ["real"]


def test_a_skill_md_inside_a_skill_is_a_file_of_it(tmp_path: Path) -> None:
    """An example SKILL.md kept under `references/` belongs to its skill; `rglob` made it a second."""
    _write(tmp_path, "outer/SKILL.md", _skill_md("outer"))
    _write(tmp_path, "outer/references/example/SKILL.md", _skill_md("example"))
    assert loader.load_skills_from_dir(tmp_path).names() == ["outer"]


def test_a_skill_at_the_root_does_not_end_the_walk(tmp_path: Path) -> None:
    """`FELIX_SKILLS_DIR` holding a SKILL.md of its own still serves the skills below it."""
    _write(tmp_path, "SKILL.md", _skill_md("rooted"))
    _write(tmp_path, "child/SKILL.md", _skill_md("child"))
    assert loader.load_skills_from_dir(tmp_path).names() == ["child", "rooted"]


def test_a_symlinked_directory_is_not_followed(tmp_path: Path) -> None:
    _write(tmp_path, "elsewhere/linked/SKILL.md", _skill_md("linked"))
    (tmp_path / "skills").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "skills" / "via-link")
    assert loader.load_skills_from_dir(tmp_path / "skills").names() == []


def test_hidden_and_dependency_directories_are_not_walked(tmp_path: Path) -> None:
    _write(tmp_path, ".git/hooks/SKILL.md", _skill_md("hooks"))
    _write(tmp_path, "pkg/node_modules/dep/SKILL.md", _skill_md("dep"))
    _write(tmp_path, "group/nested/SKILL.md", _skill_md("nested"))
    assert loader.load_skills_from_dir(tmp_path).names() == ["nested"]


def test_the_walk_is_depth_bounded(tmp_path: Path) -> None:
    """A skill directory at exactly `MAX_SCAN_DEPTH` levels loads; one level deeper does not."""
    at_limit = "/".join(f"a{i}" for i in range(discovery.MAX_SCAN_DEPTH - 1))
    past_limit = "/".join(f"d{i}" for i in range(discovery.MAX_SCAN_DEPTH))
    _write(tmp_path, f"{at_limit}/ok/SKILL.md", _skill_md("ok"))
    _write(tmp_path, f"{past_limit}/too-deep/SKILL.md", _skill_md("too-deep"))
    assert loader.load_skills_from_dir(tmp_path).names() == ["ok"]


def test_the_walk_stops_after_max_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The root counts as one directory, then its children in name order."""
    monkeypatch.setattr(discovery, "MAX_SCAN_DIRS", 3)
    for name in ("s1", "s2", "s3"):
        _write(tmp_path, f"{name}/SKILL.md", _skill_md(name))
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        catalog = loader.load_skills_from_dir(tmp_path)
    assert catalog.names() == ["s1", "s2"]
    assert "skills dir %s: stopped after %d directories" in _warnings(caplog)


def test_a_shadowed_name_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write(tmp_path, "a/twin/SKILL.md", _skill_md("twin", "First."))
    _write(tmp_path, "b/twin/SKILL.md", _skill_md("twin", "Second."))
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        catalog = loader.load_skills_from_dir(tmp_path)
    assert catalog.skills["twin"].description == "First."
    assert "shadowed" in caplog.text


async def test_a_later_host_root_shadowing_an_earlier_one_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bundled, configured = tmp_path / "bundled", tmp_path / "configured"
    _write(bundled, "twin/SKILL.md", _skill_md("twin", "Bundled."))
    _write(configured, "twin/SKILL.md", _skill_md("twin", "Configured."))
    monkeypatch.setattr(loader, "_default_bundled_dir", lambda: bundled)
    monkeypatch.setattr(loader, "_configured_skills_dir", lambda: configured)
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        host = await loader.host_catalog()
    assert host.skills["twin"].description == "Configured."  # precedence is unchanged
    assert "shadows" in caplog.text


# -- lenient loading: warn, and load ------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.msg for r in caplog.records if r.levelno == logging.WARNING and r.name == "felix.skills.loader"]


@pytest.mark.parametrize(
    ("directory", "frontmatter_name", "description", "loaded_as", "warning"),
    [
        # The frontmatter name wins: a ref resolves by it, not by the folder.
        (
            "other-dir",
            "named",
            "Fine.",
            "named",
            "skill %s: its directory is %r; the spec asks the two to match",
        ),
        ("upper", "Upper", "Fine.", "upper", "skill name %r is not lowercase; loaded as %r"),
        (
            "long",
            "long",
            "x" * 1100,
            "long",
            "skill %s: description is %d characters; the catalog lists the first %d",
        ),
    ],
    ids=["name-differs-from-directory", "name-not-lowercase", "description-too-long"],
)
def test_a_cosmetic_problem_warns_and_loads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    directory: str,
    frontmatter_name: str,
    description: str,
    loaded_as: str,
    warning: str,
) -> None:
    _write(tmp_path, f"{directory}/SKILL.md", _skill_md(frontmatter_name, description))
    with caplog.at_level(logging.WARNING, logger="felix.skills.loader"):
        catalog = loader.load_skills_from_dir(tmp_path)
    assert catalog.names() == [loaded_as]
    assert len(catalog.skills[loaded_as].description) <= 1024
    assert warning in _warnings(caplog)


def test_a_missing_description_skips_the_skill(tmp_path: Path) -> None:
    _write(tmp_path, "nodesc/SKILL.md", "---\nname: nodesc\n---\nbody\n")
    assert loader.load_skills_from_dir(tmp_path).names() == []


# -- hidden means hidden -------------------------------------------------------------------------


def _catalog_with_hidden() -> SkillCatalog:
    return SkillCatalog(
        skills={
            "shown": Skill(name="shown", description="Shown.", body="shown body"),
            "secret": Skill(
                name="secret", description="Hidden.", body="secret body", disable_model_invocation=True
            ),
            "missing": Skill(name="missing", description="(body not found)", unresolved=True),
        }
    )


@pytest.mark.parametrize("name", ["secret", "missing"])
async def test_a_skill_hidden_from_the_catalog_cannot_be_activated(name: str) -> None:
    """`disable-model-invocation` left a skill out of the listing while `activate_skill` served it
    to a model that guessed the name; the spec hides it entirely."""
    tools = _tools(_catalog_with_hidden())
    out = await _call(tools["activate_skill"], {"name": name})
    assert out == {"error": "unknown_skill", "name": name}


async def test_a_hidden_name_never_appears_in_the_active_set() -> None:
    """A thread that activated skills the catalog has since hidden: activate and deactivate report
    only names the model is offered now."""
    sessions = InMemorySessionStore(tenant_id="t")
    # On the branch, as a turn appends them, so only the catalog filter can keep them out.
    await annotate_and_append(
        sessions.open("t:1"),
        [
            chat_message_to_event(
                ChatMessage(
                    role="tool",
                    name="activate_skill",
                    tool_call_id=f"old-{n}",
                    content=json.dumps({"activated": n}),
                )
            )
            for n in ("secret", "missing")
        ],
    )
    tools = {
        t.name: t
        for t in make_skill_tools(
            _catalog_with_hidden(), session_store=sessions, tenant_id="t", manifest_id="m"
        )
    }
    activated = json.loads(
        await call_in_thread(
            tools["activate_skill"], {"name": "shown"}, session_store=sessions, thread_id="t:1"
        )
    )
    assert activated["active_skills"] == ["shown"]
    deactivated = json.loads(
        await call_in_thread(
            tools["deactivate_skill"], {"name": "secret"}, session_store=sessions, thread_id="t:1"
        )
    )
    assert deactivated == {"deactivated": "secret", "active_skills": ["shown"]}


async def test_a_hidden_skill_s_files_cannot_be_read(tmp_path: Path) -> None:
    _write(tmp_path, "secret/SKILL.md", _skill_md("secret", extra="disable-model-invocation: true\n"))
    _write(tmp_path, "secret/references/notes.md", "private")
    tools = _tools(loader.load_skills_from_dir(tmp_path))
    out = await _call(tools["read_skill_file"], {"name": "secret", "path": "references/notes.md"})
    assert out == {"error": "unknown_skill", "name": "secret"}


async def test_an_unresolved_ref_is_not_offered_to_the_model() -> None:
    catalog = await loader.load_manifest_skills(
        [{"name": "nowhere"}], bundled_dir=Path("/nonexistent"), declared_only=True, owner=None
    )
    assert catalog.skills["nowhere"].unresolved  # still there for the management API
    assert loader.skill_catalog_xml(catalog) == ""
    listed = await _call(_tools(catalog)["list_skills"], {})
    assert listed == []


def test_the_name_argument_enumerates_only_invocable_skills() -> None:
    tools = _tools(_catalog_with_hidden())
    for tool_name in ("activate_skill", "deactivate_skill", "read_skill_file"):
        schema = tool_input_schema(tools[tool_name])
        assert schema["properties"]["name"]["enum"] == ["shown"], tool_name


@pytest.mark.parametrize(
    "catalog",
    [
        SkillCatalog(),
        SkillCatalog(skills={"x": Skill(name="x", description="X.", disable_model_invocation=True)}),
        SkillCatalog(skills={f"s{i}": Skill(name=f"s{i}", description="S.") for i in range(65)}),
    ],
    ids=["empty", "only-hidden", "past-the-enum-cap"],
)
def test_no_enum_when_no_skill_is_invocable(catalog: SkillCatalog) -> None:
    """`enum: []` is a schema no value satisfies, and some providers refuse it; a long list costs
    more on every request than it helps."""
    tools = _tools(catalog)
    for tool_name in ("activate_skill", "deactivate_skill", "read_skill_file"):
        assert "enum" not in tool_input_schema(tools[tool_name])["properties"]["name"], tool_name


# -- the catalog block and activation ------------------------------------------------------------


def test_the_catalog_is_ordered_by_name_and_says_where_paths_resolve() -> None:
    catalog = SkillCatalog(
        skills={n: Skill(name=n, description=f"{n} skill.") for n in ("zeta", "alpha", "mid")}
    )
    xml = loader.skill_catalog_xml(catalog)
    assert xml.index('"alpha"') < xml.index('"mid"') < xml.index('"zeta"')
    assert "read_skill_file" in xml


async def test_activation_reports_the_informational_frontmatter() -> None:
    skill = Skill(
        name="pdf",
        description="PDFs.",
        body="Do PDFs.",
        metadata={"license": "Apache-2.0", "compatibility": "Requires uv", "allowed-tools": "Bash(uv:*)"},
    )
    out = await _call(_tools(SkillCatalog(skills={"pdf": skill}))["activate_skill"], {"name": "pdf"})
    assert isinstance(out, dict)
    assert (out["license"], out["compatibility"], out["allowed_tools"]) == (
        "Apache-2.0",
        "Requires uv",
        "Bash(uv:*)",
    )


async def test_an_oversized_body_is_cut_and_says_so() -> None:
    skill = Skill(name="big", description="Big.", body="x" * (MAX_ACTIVATION_CHARS + 10))
    out = await _call(_tools(SkillCatalog(skills={"big": skill}))["activate_skill"], {"name": "big"})
    assert isinstance(out, dict)
    assert len(out["instructions"]) == MAX_ACTIVATION_CHARS and out["truncated"] is True


async def test_a_long_bundle_listing_says_it_was_cut(tmp_path: Path) -> None:
    _write(tmp_path, "many/SKILL.md", _skill_md("many"))
    for i in range(MAX_BUNDLE_FILES + 5):
        _write(tmp_path, f"many/references/r{i:03}.md", "r")
    tools = _tools(loader.load_skills_from_dir(tmp_path))
    out = await _call(tools["activate_skill"], {"name": "many"})
    assert isinstance(out, dict)
    assert len(out["files"]) == MAX_BUNDLE_FILES and out["files_truncated"] is True


async def test_a_symlink_leading_out_of_the_skill_is_not_listed(tmp_path: Path) -> None:
    """Listed and then refused by `read_skill_file`; now neither."""
    _write(tmp_path, "outside.md", "secret")
    _write(tmp_path, "skills/linky/SKILL.md", _skill_md("linky"))
    _write(tmp_path, "skills/linky/references/inside.md", "fine")
    os.symlink(tmp_path / "outside.md", tmp_path / "skills/linky/references/escape.md")
    os.symlink(tmp_path / "skills/linky/references/inside.md", tmp_path / "skills/linky/references/alias.md")
    tools = _tools(loader.load_skills_from_dir(tmp_path / "skills"))
    out = await _call(tools["activate_skill"], {"name": "linky"})
    assert isinstance(out, dict)
    assert out["files"] == ["references/alias.md", "references/inside.md"]


# -- the strict check ----------------------------------------------------------------------------


def test_a_byte_order_mark_does_not_fail_validation() -> None:
    """The catalog loaded a BOM-prefixed SKILL.md that the save path refused."""
    result = validate_skill_bundle({"SKILL.md": "\ufeff" + _skill_md("bom")}, expected_slug="bom")
    assert result.valid, result.errors
    assert split_frontmatter("\ufeff" + _skill_md("bom")) is not None


def test_validate_accepts_a_good_skill(tmp_path: Path) -> None:
    _write(tmp_path, "good/SKILL.md", _skill_md("good"))
    _write(tmp_path, "good/references/guide.md", "guide")
    result = CliRunner().invoke(cli, ["skills", "validate", str(tmp_path / "good")])
    assert result.exit_code == 0, result.output
    assert "Valid skill" in result.output


@pytest.mark.parametrize(
    ("directory", "skill_md", "issue_path"),
    [
        ("mismatch", _skill_md("other"), "frontmatter.name"),
        ("bad--name", _skill_md("bad--name"), "frontmatter.name"),
        ("nodesc", "---\nname: nodesc\n---\nbody\n", "frontmatter.description"),
    ],
    ids=["name-differs-from-directory", "consecutive-hyphens", "no-description"],
)
def test_validation_refuses_a_bad_skill(directory: str, skill_md: str, issue_path: str) -> None:
    result = validate_skill_bundle({"SKILL.md": skill_md}, expected_slug=directory)
    assert not result.valid
    assert issue_path in {issue.path for issue in result.errors}


def test_validate_exits_1_and_lists_the_issues(tmp_path: Path) -> None:
    _write(tmp_path, "bad--name/SKILL.md", _skill_md("bad--name"))
    result = CliRunner().invoke(cli, ["skills", "validate", str(tmp_path / "bad--name" / "SKILL.md")])
    assert result.exit_code == 1
    assert "Validation failed for" in result.output and "frontmatter.name" in result.output


def test_validate_skips_hidden_files(tmp_path: Path) -> None:
    """Finder's `.DS_Store` would otherwise fail every skill directory on a Mac."""
    _write(tmp_path, "tidy/SKILL.md", _skill_md("tidy"))
    (tmp_path / "tidy" / ".DS_Store").write_bytes(b"\x00\x01")
    result = CliRunner().invoke(cli, ["skills", "validate", str(tmp_path / "tidy")])
    assert result.exit_code == 0, result.output


def test_validate_reports_text_that_is_not_utf8(tmp_path: Path) -> None:
    _write(tmp_path, "enc/SKILL.md", _skill_md("enc"))
    (tmp_path / "enc" / "references").mkdir()
    (tmp_path / "enc" / "references" / "latin1.md").write_bytes(b"caf\xe9")
    result = CliRunner().invoke(cli, ["skills", "validate", str(tmp_path / "enc")])
    assert result.exit_code == 1
    assert "references/latin1.md: not UTF-8 text" in result.output and "Valid skill" not in result.output


def test_validate_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["skills", "validate", str(tmp_path / "absent")])
    assert result.exit_code == 1 and "Not a directory" in result.output
