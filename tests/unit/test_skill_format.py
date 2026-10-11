"""`felix.skills.format`, `binary`, `plugin` and `semver`.

Every bundle in `tests/fixtures/skills/` must validate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from felix.skills.binary import (
    MAX_BINARY_ASSET_BYTES,
    base64_decoded_size,
    binary_asset_mime_type,
    decode_base64,
    encode_base64,
    is_binary_asset_path,
    is_valid_base64,
)
from felix.skills.format import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FRONTMATTER_CHARS,
    MAX_SKILL_MD_CHARS,
    SkillFrontmatter,
    ValidationIssue,
    create_skill_template,
    extract_discovery_meta,
    is_valid_skill_name,
    parse_skill_md,
    serialize_skill_md,
    update_skill_md_frontmatter,
    validate_skill_bundle,
    validate_skill_name,
)
from felix.skills.plugin import PluginManifest, is_allowed_host_pattern, parse_plugin_manifest
from felix.skills.semver import bump_semver, compare_semver, resolve_next_semver
from pydantic import ValidationError

from tests.support import paths

FIXTURES = paths.FIXTURES / "skills"


def _bundle_dirs() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if p.is_dir())


def _read_bundle(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()
    }


def _messages(files: dict[str, str], slug: str | None = None) -> list[str]:
    return [e.message for e in validate_skill_bundle(files, slug).errors]


# --- validate_skill_name ----------------------------------------------------------------


def test_skill_name_accepts_a_valid_name() -> None:
    assert validate_skill_name("pdf-processing", "pdf-processing") == []


_CHARS = (
    "name may only contain lowercase letters, numbers, and hyphens; no leading/trailing/consecutive hyphens"
)
_LENGTH = "name must be 1-64 characters"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("PDF-processing", _CHARS),
        ("-lead", _CHARS),
        ("trail-", _CHARS),
        ("dou--ble", _CHARS),
        ("has space", _CHARS),
        ("", _LENGTH),
        ("x" * 65, _LENGTH),
    ],
)
def test_skill_name_rejects(name: str, message: str) -> None:
    """One issue per bad name, however many ways it is bad."""
    assert validate_skill_name(name) == [ValidationIssue("name", message)]
    assert not is_valid_skill_name(name)


def test_the_name_rule_has_one_definition() -> None:
    """The model, `validate_skill_name` and the loader's warning all ask `is_valid_skill_name`."""
    for name in ["a", "a-b", "x" * 64, "a1-2b"]:
        assert is_valid_skill_name(name)
        assert SkillFrontmatter.model_validate({"name": name, "description": "d"}).name == name


def test_skill_name_rejects_a_slug_mismatch() -> None:
    assert validate_skill_name("other", "pdf-processing") == [
        ValidationIssue("name", 'name must match skill slug "pdf-processing"')
    ]


# --- validate_skill_bundle --------------------------------------------------------------


def test_a_minimal_template_validates() -> None:
    result = validate_skill_bundle(create_skill_template("roll-dice", "Roll dice when asked."), "roll-dice")
    assert result.valid, result.errors
    assert result.frontmatter is not None and result.frontmatter.name == "roll-dice"


def test_a_description_containing_a_colon_survives_the_template() -> None:
    """A default description such as "Agent skill: <name>" must not be interpolated raw."""
    result = validate_skill_bundle(create_skill_template("my-skill", "Agent skill: my skill"), "my-skill")
    assert result.valid, result.errors
    assert result.frontmatter is not None and result.frontmatter.description == "Agent skill: my skill"


def test_skill_md_is_required() -> None:
    assert _messages({}) == ["SKILL.md is required"]


_NO_FRONTMATTER = (
    "SKILL.md must contain YAML frontmatter delimited by --- "
    "(within the size and nesting limits, with no anchors or aliases)"
)


@pytest.mark.parametrize(
    "skill_md",
    [
        "# No frontmatter",
        "---\nname: a\nname: b\ndescription: d\n---\nbody",  # a duplicate key
        "---\nname: ok\ndescription: &d text\nlicense: *d\n---\nbody",  # an anchor and alias
        "---\nname: ok\ndescription: d\nx: " + "[" * 1000 + "]" * 1000 + "\n---\nbody",  # depth
        "---\nname: ok\ndescription: d\nx: " + "y" * MAX_FRONTMATTER_CHARS + "\n---\nbody",  # size
    ],
    ids=["missing", "duplicate-key", "alias", "deep", "oversized"],
)
def test_frontmatter_yaml_refuses(skill_md: str) -> None:
    assert parse_skill_md(skill_md) is None
    assert _messages({"SKILL.md": skill_md}) == [_NO_FRONTMATTER]


def test_a_refused_parse_does_not_break_the_next_one() -> None:
    """ruamel's composer depth never unwinds after a depth error on a reused instance."""
    for _ in range(3):
        assert parse_skill_md("---\nx: " + "[" * 100 + "]" * 100 + "\n---\n") is None
        assert parse_skill_md("---\nname: a\n---\n") is not None


def test_frontmatter_schema_errors_are_reported_once_per_field() -> None:
    result = validate_skill_bundle(
        {"SKILL.md": "---\nname: Bad_Name\ndescription: ''\n---\nbody"}, "bad-name"
    )
    assert not result.valid
    assert result.frontmatter is None
    assert [(e.path, e.message) for e in result.errors] == [
        ("frontmatter.name", _CHARS),
        ("frontmatter.description", "String should have at least 1 character"),
        ("frontmatter.name", 'name must match skill slug "bad-name"'),
    ]


def test_the_slug_is_checked_against_the_name_as_written() -> None:
    """Even when the model refused the frontmatter, the name compared is the one in the file."""
    files = {"SKILL.md": "---\nname: right\ndescription: ''\n---\nbody"}
    assert ("frontmatter.name", 'name must match skill slug "right"') not in [
        (e.path, e.message) for e in validate_skill_bundle(files, "right").errors
    ]


@pytest.mark.parametrize(
    ("field", "value", "loc"),
    [
        ("description", "x" * 1025, ("description",)),
        ("compatibility", "x" * 501, ("compatibility",)),
        ("metadata", {"version": 1.2}, ("metadata", "version")),
        ("allowed-tools", ["Bash", "Read"], ("allowed-tools",)),
        ("license", 3, ("license",)),
        ("name", "Upper", ("name",)),
        ("hooks", {"pre": {"run": "x"}}, ("hooks",)),
        ("tags", [["a"]], ("tags",)),
    ],
)
def test_frontmatter_field_limits(field: str, value: object, loc: tuple[str, ...]) -> None:
    frontmatter = {"name": "ok", "description": "fine", field: value}
    with pytest.raises(ValidationError) as exc:
        SkillFrontmatter.model_validate(frontmatter)
    assert [e["loc"] for e in exc.value.errors()] == [loc]


def test_unknown_keys_may_hold_flat_values() -> None:
    fm = SkillFrontmatter.model_validate(
        {"name": "ok", "description": "d", "tags": ["a", 1], "hooks": {"pre": "x"}, "n": None}
    )
    assert fm.model_extra == {"tags": ["a", 1], "hooks": {"pre": "x"}, "n": None}


def test_a_trailing_space_on_a_fence_is_still_frontmatter() -> None:
    parsed = parse_skill_md("---  \nname: a\ndescription: d\n---\t\nbody")
    assert parsed is not None and parsed.frontmatter == {"name": "a", "description": "d"}


def test_valid_is_derived_and_cannot_disagree() -> None:
    assert not validate_skill_bundle({}).valid
    result = validate_skill_bundle(create_skill_template("ok", "d"))
    assert result.valid and result.frontmatter is not None and result.errors == []


def test_frontmatter_keeps_unknown_keys() -> None:
    fm = SkillFrontmatter.model_validate({"name": "ok", "description": "d", "version": "1.0"})
    assert fm.model_dump(by_alias=True, exclude_none=True) == {
        "name": "ok",
        "description": "d",
        "version": "1.0",
    }


def test_a_metadata_date_stays_a_string() -> None:
    """YAML 1.2 core (what the TypeScript reads) has no timestamp type."""
    files = {"SKILL.md": "---\nname: ok\ndescription: d\nmetadata:\n  updated: 2026-01-01\n---\nbody"}
    result = validate_skill_bundle(files)
    assert result.valid, result.errors
    assert result.frontmatter is not None and result.frontmatter.metadata == {"updated": "2026-01-01"}


def test_plugin_json_is_allowed_at_the_root() -> None:
    bundle = create_skill_template("my-skill", "A skill with plugin manifest for editor integration.")
    bundle["plugin.json"] = json.dumps({"name": "my-skill", "skills": ["SKILL.md"]})
    assert validate_skill_bundle(bundle, "my-skill").valid


_UNEXPECTED = "unexpected file path; use scripts/, references/, assets/, or evals/"
_SEGMENT = "each path segment must be 1-128 characters of A-Z, a-z, 0-9, '.', '_' or '-'"
_ESCAPE = "file path must be relative and must not contain '..' or a leading slash"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("notes.md", _UNEXPECTED),
        ("README.md", _UNEXPECTED),
        # Stricter than a validator that accepts any subdirectory or root file.
        ("scripts", _UNEXPECTED),
        ("docs/guide.md", _UNEXPECTED),
        ("Scripts/run.sh", _UNEXPECTED),  # case-sensitive
        ("PLUGIN.JSON", _UNEXPECTED),
        ("scripts/SKILL.md", "SKILL.md belongs only at the bundle root"),
        ("scripts/run me.sh", _SEGMENT),
        ("scripts/café.sh", _SEGMENT),
        ("scripts/a\tb.sh", _SEGMENT),
        ("scripts/" + "a" * 129, _SEGMENT),
    ],
)
def test_a_path_outside_the_allowlist_is_rejected(path: str, message: str) -> None:
    bundle = create_skill_template("my-skill", "A skill.")
    bundle[path] = "x"
    assert validate_skill_bundle(bundle, "my-skill").errors == [ValidationIssue(path, message)]


@pytest.mark.parametrize(
    "path",
    ["scripts/run.sh", "references/a/b.md", "assets/t.json", "evals/cases.json", "scripts/" + "a" * 128],
)
def test_files_in_the_bundle_directories_are_accepted(path: str) -> None:
    bundle = create_skill_template("my-skill", "A skill.")
    bundle[path] = "x"
    assert validate_skill_bundle(bundle, "my-skill").valid


def test_a_valid_binary_asset_under_assets_is_accepted() -> None:
    bundle = create_skill_template("my-skill", "A skill with a logo asset.")
    bundle["assets/logo.png"] = encode_base64(bytes([137, 80, 78, 71]))
    assert validate_skill_bundle(bundle, "my-skill").valid


def test_a_binary_asset_outside_assets_is_rejected() -> None:
    bundle = create_skill_template("my-skill", "A skill with a misplaced asset.")
    bundle["scripts/logo.png"] = encode_base64(bytes([1, 2, 3]))
    assert (
        ValidationIssue("scripts/logo.png", "binary assets (images, PDFs, archives) must live under assets/")
        in validate_skill_bundle(bundle, "my-skill").errors
    )


def test_invalid_base64_for_a_binary_asset_is_rejected() -> None:
    bundle = create_skill_template("my-skill", "A skill with a corrupt asset.")
    bundle["assets/logo.png"] = "not valid base64!!"
    assert any("valid base64" in m for m in _messages(bundle, "my-skill"))


def test_a_binary_asset_over_the_size_limit_is_rejected() -> None:
    bundle = create_skill_template("my-skill", "A skill with an oversized asset.")
    # Just over the asset cap and under the 8 MiB bundle cap, so the asset rule is what fires.
    bundle["assets/huge.png"] = encode_base64(bytes(MAX_BINARY_ASSET_BYTES + 3))
    assert any("exceeds the 5MB limit" in m for m in _messages(bundle, "my-skill"))


def test_a_binary_asset_at_the_size_limit_is_accepted() -> None:
    bundle = create_skill_template("my-skill", "A skill with a large asset.")
    bundle["assets/big.png"] = encode_base64(bytes(MAX_BINARY_ASSET_BYTES))
    assert validate_skill_bundle(bundle, "my-skill").valid


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../../etc/cron.d/evil", _ESCAPE),
        ("scripts/../../x.sh", _ESCAPE),
        ("scripts/./x.sh", _ESCAPE),
        ("scripts//x.sh", _ESCAPE),
        ("/etc/passwd", _ESCAPE),
        ("\\windows\\system32", _ESCAPE),
        ("scripts\\..\\x.sh", _ESCAPE),
        ("C:/Windows/x", _SEGMENT),
        ("scripts/a\0b.sh", _SEGMENT),
    ],
)
def test_an_escaping_path_is_rejected_rather_than_skipped(path: str, message: str) -> None:
    bundle = create_skill_template("my-skill", "A skill that tries to escape its root.")
    bundle[path] = "* * * * * root sh"
    result = validate_skill_bundle(bundle, "my-skill")
    assert not result.valid
    assert ValidationIssue(path, message) in result.errors


def test_bundle_size_caps() -> None:
    base = create_skill_template("my-skill", "A skill.")
    too_many = base | {f"references/{i}.md": "x" for i in range(MAX_BUNDLE_FILES)}
    assert _messages(too_many) == [f"a bundle may hold at most {MAX_BUNDLE_FILES} files"]
    too_big = base | {f"references/{i}.md": "x" * (MAX_BUNDLE_BYTES // 4) for i in range(5)}
    assert _messages(too_big) == ["a bundle may total at most 8 MiB"]
    long_md = {"SKILL.md": base["SKILL.md"] + "x" * MAX_SKILL_MD_CHARS}
    assert _messages(long_md) == ["SKILL.md may be at most 256 KiB"]
    at_limit = base | {f"references/{i}.md": "x" for i in range(MAX_BUNDLE_FILES - 1)}
    assert validate_skill_bundle(at_limit).valid


@pytest.mark.parametrize("bundle_dir", _bundle_dirs(), ids=lambda p: p.name)
def test_every_example_bundle_validates(bundle_dir: Path) -> None:
    result = validate_skill_bundle(_read_bundle(bundle_dir), bundle_dir.name)
    assert result.valid, result.errors


def test_the_example_fixtures_are_present() -> None:
    """The parametrized case above passes vacuously over an empty directory."""
    assert len(_bundle_dirs()) >= 10


def test_extract_discovery_meta() -> None:
    bundle = create_skill_template("my-skill", "Finds things.")
    assert extract_discovery_meta(bundle) == ("my-skill", "Finds things.")
    assert extract_discovery_meta({"SKILL.md": "nope"}) is None


# --- parse_skill_md ---------------------------------------------------------------------


def test_parse_splits_yaml_text_frontmatter_and_body() -> None:
    parsed = parse_skill_md("---\nname: roll-dice\ndescription: Roll dice.\n---\n# Body\n")
    assert parsed is not None
    assert parsed.yaml_text == "name: roll-dice\ndescription: Roll dice."
    assert parsed.frontmatter == {"name": "roll-dice", "description": "Roll dice."}
    assert parsed.body == "# Body\n"


def test_parse_handles_crlf_fences() -> None:
    parsed = parse_skill_md("---\r\nname: roll-dice\r\ndescription: Roll dice.\r\n---\r\n# Body")
    assert parsed is not None and parsed.body == "# Body"


@pytest.mark.parametrize(
    "content",
    [
        "# no frontmatter",
        "---\n: [broken\n---\nbody",
        "---\nname: a\nname: b\n---\nbody",
        "---\nname: a\n---",
    ],
)
def test_parse_returns_none_on_missing_fences_or_invalid_yaml(content: str) -> None:
    assert parse_skill_md(content) is None


# --- serialize / update -----------------------------------------------------------------


def test_serialize_round_trips_with_stable_key_order() -> None:
    fm = {
        "allowed-tools": "Bash Read",
        "description": "Roll dice.",
        "metadata": {"author": "felix"},
        "name": "roll-dice",
    }
    content = serialize_skill_md(fm, "# Body\n")
    assert content == (
        "---\nname: roll-dice\ndescription: Roll dice.\nmetadata:\n  author: felix\n"
        "allowed-tools: Bash Read\n---\n# Body\n"
    )
    parsed = parse_skill_md(content)
    assert parsed is not None and parsed.frontmatter == fm and parsed.body == "# Body\n"


def test_serialize_does_not_fold_long_values() -> None:
    description = (
        "Extracts text and tables from PDF files, fills PDF forms, and merges multiple PDFs. Use when "
        "working with PDF documents or when the user mentions PDFs, forms, or document extraction."
    )
    content = serialize_skill_md({"name": "pdf-tools", "description": description}, "")
    assert content == f"---\nname: pdf-tools\ndescription: {description}\n---\n"
    parsed = parse_skill_md(content)
    assert parsed is not None and parsed.frontmatter == {"name": "pdf-tools", "description": description}


def test_serialize_omits_none_fields() -> None:
    assert (
        serialize_skill_md({"name": "a", "description": "b", "license": None}, "")
        == "---\nname: a\ndescription: b\n---\n"
    )


def test_serialize_quotes_as_the_typescript_does() -> None:
    """Double quotes, so a SKILL.md saved by Felix or by the vendored TS gives the same bytes."""
    fm = {"name": "a", "description": "Agent skill: a", "metadata": {"n": "1.0", "e": ""}}
    assert serialize_skill_md(fm, "") == (
        '---\nname: a\ndescription: "Agent skill: a"\nmetadata:\n  n: "1.0"\n  e: ""\n---\n'
    )


def test_serialize_accepts_the_model_and_keeps_unknown_keys_last() -> None:
    fm = SkillFrontmatter.model_validate(
        {"version": "2", "allowed-tools": "Read", "name": "a", "description": "b", "license": "MIT"}
    )
    assert serialize_skill_md(fm, "") == (
        '---\nname: a\ndescription: b\nlicense: MIT\nallowed-tools: Read\nversion: "2"\n---\n'
    )


def test_update_keeps_the_body_byte_identical() -> None:
    body = "# Title\n\n```py\nx = 1\n```\n\ntrailing  spaces  \n"
    content = serialize_skill_md({"name": "a", "description": "b"}, body)
    parsed = parse_skill_md(update_skill_md_frontmatter(content, {"name": "a", "description": "changed"}))
    assert parsed is not None
    assert parsed.body == body
    assert parsed.frontmatter == {"name": "a", "description": "changed"}


def test_update_drops_yaml_comments() -> None:
    updated = update_skill_md_frontmatter(
        "---\n# a comment\nname: a\ndescription: b\n---\nbody", {"name": "a", "description": "b"}
    )
    assert "# a comment" not in updated
    parsed = parse_skill_md(updated)
    assert parsed is not None and parsed.body == "body"


def test_update_wraps_content_lacking_frontmatter() -> None:
    parsed = parse_skill_md(update_skill_md_frontmatter("just a body", {"name": "a", "description": "b"}))
    assert parsed is not None and parsed.body == "just a body"


# --- README example (readme-example.test.ts) --------------------------------------------

_README_BUNDLE = {
    "SKILL.md": """---
name: my-skill
description: Audits a codebase and reports findings.
metadata:
  category: quality
  tags: audit, review
---
# My Skill

Instructions for the agent go here.""",
}


def test_the_documented_sample_bundle_validates() -> None:
    result = validate_skill_bundle(_README_BUNDLE, "my-skill")
    assert result.errors == []
    assert result.valid
    assert result.frontmatter is not None
    assert result.frontmatter.metadata == {"category": "quality", "tags": "audit, review"}


# --- binary -----------------------------------------------------------------------------


def test_binary_asset_paths() -> None:
    assert is_binary_asset_path("assets/logo.png")
    assert is_binary_asset_path("assets/Logo.PNG")
    assert is_binary_asset_path("assets/manual.pdf")
    assert not is_binary_asset_path("assets/data.json")
    assert not is_binary_asset_path("SKILL.md")


def test_binary_asset_mime_types() -> None:
    assert binary_asset_mime_type("assets/logo.png") == "image/png"
    assert binary_asset_mime_type("assets/photo.JPG") == "image/jpeg"
    assert binary_asset_mime_type("assets/mystery.bin") == "application/octet-stream"


def test_base64_round_trips_every_byte_value() -> None:
    data = bytes(range(256))
    encoded = encode_base64(data)
    assert is_valid_base64(encoded)
    assert decode_base64(encoded) == data


def test_base64_handles_large_and_empty_input() -> None:
    assert len(decode_base64(encode_base64(bytes([7]) * 2_000_000))) == 2_000_000
    assert decode_base64(encode_base64(b"")) == b""


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 5, 100, 4095])
def test_base64_decoded_size_matches_the_real_length(length: int) -> None:
    assert base64_decoded_size(encode_base64(bytes([1]) * length)) == length


@pytest.mark.parametrize(
    ("value", "valid"), [("not base64!!", False), ("abc", False), ("aGVsbG8=", True), ("", True)]
)
def test_is_valid_base64(value: str, valid: bool) -> None:
    assert is_valid_base64(value) is valid


def test_max_binary_asset_is_five_mib() -> None:
    assert MAX_BINARY_ASSET_BYTES == 5 * 1024 * 1024


# --- plugin.json and the egress allowlist -----------------------------------------------


def test_parse_plugin_manifest() -> None:
    manifest = parse_plugin_manifest(json.dumps({"name": "my-plugin", "skills": ["SKILL.md"]}))
    assert manifest is not None and manifest.name == "my-plugin"
    defaulted = parse_plugin_manifest(json.dumps({"name": "x"}))
    assert defaulted is not None and defaulted.skills == ["SKILL.md"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        json.dumps({"name": ""}),
        json.dumps({"name": "x", "agents": [""]}),
        json.dumps({"name": "x", "mcp": {"servers": [{"name": "s", "url": "not a url"}]}}),
    ],
)
def test_parse_plugin_manifest_rejects(raw: str) -> None:
    assert parse_plugin_manifest(raw) is None


@pytest.mark.parametrize(
    "host", ["api.stripe.com", "github.com", "*.example.com", "*.githubusercontent.com", "registry.npmjs.org"]
)
def test_allowed_host_pattern_accepts_concrete_hosts_and_specific_wildcards(host: str) -> None:
    assert is_allowed_host_pattern(host)


@pytest.mark.parametrize("host", ["*", "*.*", "**", "*.com", "*.io", "", "   ", "localhost", "not a host", 7])
def test_allowed_host_pattern_rejects_catch_alls_and_tld_wildcards(host: object) -> None:
    assert not is_allowed_host_pattern(host)


def test_plugin_manifest_accepts_a_valid_allowlist() -> None:
    manifest = PluginManifest.model_validate(
        {"name": "x", "network": {"allowedHosts": ["api.stripe.com", "*.example.com"]}}
    )
    assert manifest.network is not None and manifest.network.allowed_hosts == [
        "api.stripe.com",
        "*.example.com",
    ]


@pytest.mark.parametrize("hosts", [["*"], ["*.com"], [f"h{i}.example.com" for i in range(51)]])
def test_plugin_manifest_rejects_a_catch_all_or_oversized_allowlist(hosts: list[str]) -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"name": "x", "network": {"allowedHosts": hosts}})


# --- semver -----------------------------------------------------------------------------


def test_bump_patch_by_default() -> None:
    assert bump_semver("1.2.3") == "1.2.4"


def test_bump_minor_and_major() -> None:
    assert bump_semver("1.2.3", "minor") == "1.3.0"
    assert bump_semver("1.2.3", "major") == "2.0.0"


def test_bump_tolerates_short_and_non_numeric_versions() -> None:
    assert bump_semver("1") == "1.0.1"
    assert bump_semver("x.y.z") == "0.0.1"


def test_resolve_next_semver() -> None:
    assert resolve_next_semver("2.0.0", bump="minor") == "2.1.0"
    assert resolve_next_semver("2.0.0", semver="3.0.0") == "3.0.0"
    assert resolve_next_semver("2.0.0") == "2.0.1"
    assert resolve_next_semver(None) == "0.1.0"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("1.2.3", "1.2.3", 0),
        ("1.2.3", "1.2.4", -1),
        ("1.10.0", "1.9.0", 1),
        ("2.0.0", "10.0.0", -1),
        ("1.2.3-rc.1", "1.2.3", 0),
        ("1.2.3+build", "1.2.3", 0),
        ("1.2", "1.2.0", 0),
    ],
)
def test_compare_semver(a: str, b: str, expected: int) -> None:
    assert compare_semver(a, b) == expected
