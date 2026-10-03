"""The SKILL.md format (agentskills.io): YAML frontmatter, bundle layout, templates.

`parse_skill_md` splits a SKILL.md into its frontmatter and body without judging either;
`validate_skill_bundle` is the strict check an authoring path applies before a bundle is
saved. The catalog loader (`skills/loader.py`) reads leniently on top of the parser and
never applies the strict check — a skill already on disk keeps loading.

Pure: no I/O and no settings. Ported from Skillist's `skill-format` package (MIT); see
NOTICE. Lengths are counted in code points, where the TypeScript counts UTF-16 units.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.emitter import Emitter
from ruamel.yaml.error import YAMLError

from felix.skills.binary import (
    MAX_BINARY_ASSET_BYTES,
    base64_decoded_size,
    is_binary_asset_path,
    is_valid_base64,
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_NAME_MESSAGE = "name must be lowercase alphanumeric with hyphens"

OPTIONAL_DIRS = ("scripts", "references", "assets")
ALLOWED_ROOT_FILES = ("plugin.json",)

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")

# Known keys first, in this order; anything else after, in the order given.
_KEY_ORDER = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")


class _Constructor(SafeConstructor):
    """Safe loading, with timestamps left as strings.

    The YAML 1.2 core schema the TypeScript `yaml` package reads has no timestamp type, so
    `updated: 2026-01-01` in `metadata` is a string there and a valid frontmatter value.
    ruamel would hand back a `date` and fail the `dict[str, str]` check on the same file.
    """


_Constructor.add_constructor("tag:yaml.org,2002:timestamp", _Constructor.construct_yaml_str)

_loader = YAML(typ="safe", pure=True)
_loader.Constructor = _Constructor


class _Emitter(Emitter):
    """Double quotes where ruamel would choose single ones, as the TypeScript `yaml` does,
    so a SKILL.md saved from either side serialises to the same bytes."""

    def choose_scalar_style(self) -> Any:
        style = super().choose_scalar_style()
        return '"' if style == "'" else style


_dumper = YAML(typ="safe", pure=True)
_dumper.Emitter = _Emitter
_dumper.default_flow_style = False
_dumper.allow_unicode = True
# No folding: a long description stays one line, so editing any other field does not
# reformat text the author did not touch.
_dumper.width = sys.maxsize
# The safe representer sorts keys by default, which would undo `_KEY_ORDER`.
_dumper.sort_base_mapping_type_on_output = False


def _skill_name(value: str) -> str:
    if not SKILL_NAME_RE.match(value):
        raise ValueError(_NAME_MESSAGE)
    return value


class SkillFrontmatter(BaseModel):
    """The agentskills.io frontmatter. Extra keys are kept, so a read-modify-write of a
    SKILL.md does not drop a field this model does not know about."""

    model_config = ConfigDict(extra="allow", validate_by_name=True, validate_by_alias=True)

    name: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_skill_name)]
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    compatibility: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str] | None = None
    allowed_tools: str | None = Field(default=None, alias="allowed-tools")


@dataclass(slots=True, frozen=True)
class ParsedSkillMd:
    yaml_text: str
    frontmatter: Any
    body: str


@dataclass(slots=True, frozen=True)
class ValidationIssue:
    path: str
    message: str


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    frontmatter: SkillFrontmatter | None = None
    body: str | None = None
    errors: list[ValidationIssue] = field(default_factory=list)


def split_frontmatter(content: str) -> tuple[str, str] | None:
    """(yaml text, body) between the `---` fences, or None when there are none."""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_skill_md(content: str) -> ParsedSkillMd | None:
    """Split a SKILL.md; None when the fences are missing or the YAML does not parse."""
    parts = split_frontmatter(content)
    if parts is None:
        return None
    yaml_text, body = parts
    try:
        frontmatter = _loader.load(yaml_text)
    except YAMLError:
        return None
    return ParsedSkillMd(yaml_text=yaml_text, frontmatter=frontmatter, body=body)


def _as_dict(frontmatter: SkillFrontmatter | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(frontmatter, SkillFrontmatter):
        return frontmatter.model_dump(by_alias=True, exclude_none=True)
    return {k: v for k, v in frontmatter.items() if v is not None}


def _stringify_frontmatter(frontmatter: SkillFrontmatter | Mapping[str, Any]) -> str:
    data = _as_dict(frontmatter)
    ordered = {key: data[key] for key in _KEY_ORDER if key in data}
    ordered.update((key, value) for key, value in data.items() if key not in ordered)
    out = io.StringIO()
    _dumper.dump(ordered, out)
    return out.getvalue().rstrip()


def serialize_skill_md(frontmatter: SkillFrontmatter | Mapping[str, Any], body: str) -> str:
    return f"---\n{_stringify_frontmatter(frontmatter)}\n---\n{body}"


def update_skill_md_frontmatter(content: str, frontmatter: SkillFrontmatter | Mapping[str, Any]) -> str:
    """Regenerate only the YAML between the fences; the body stays byte-identical.

    Comments and custom key order inside the YAML block are not preserved. Content with
    no frontmatter becomes the body of a new SKILL.md.
    """
    parts = split_frontmatter(content)
    if parts is None:
        return serialize_skill_md(frontmatter, content)
    return serialize_skill_md(frontmatter, parts[1])


def validate_skill_name(name: str, slug: str | None = None) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    if not 1 <= len(name) <= 64:
        errors.append(ValidationIssue("name", "name must be 1-64 characters"))
    if not SKILL_NAME_RE.match(name):
        errors.append(
            ValidationIssue(
                "name",
                "name may only contain lowercase letters, numbers, and hyphens; "
                "no leading/trailing/consecutive hyphens",
            )
        )
    if name.startswith("-") or name.endswith("-") or "--" in name:
        errors.append(
            ValidationIssue("name", "name must not start/end with hyphen or contain consecutive hyphens")
        )
    if slug and name != slug:
        errors.append(ValidationIssue("name", f'name must match skill slug "{slug}"'))
    return errors


def _frontmatter_issues(exc: ValidationError) -> list[ValidationIssue]:
    issues = []
    for err in exc.errors():
        message = str(err["msg"]).removeprefix("Value error, ")
        issues.append(ValidationIssue(f"frontmatter.{'.'.join(str(p) for p in err['loc'])}", message))
    return issues


def _unsafe_path(path: str) -> bool:
    # Rejected outright, never folded into the allowed-root skip: a bundle entry is
    # eventually written under some root, and `..` or a leading slash escapes it.
    return ".." in path or path.startswith(("/", "\\")) or "\0" in path or bool(_DRIVE_RE.match(path))


def _file_issues(path: str, content: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if is_binary_asset_path(path) and not path.startswith("assets/"):
        issues.append(ValidationIssue(path, "binary assets (images, PDFs, archives) must live under assets/"))
    if _unsafe_path(path):
        issues.append(
            ValidationIssue(path, "file path must be relative and must not contain '..' or a leading slash")
        )
        return issues
    if path in ALLOWED_ROOT_FILES:
        return issues
    # Only a root-level file is checked (a bare `scripts` included, as in the original); a
    # file in an unlisted subdirectory passes.
    if path and "/" not in path and path not in OPTIONAL_DIRS:
        issues.append(ValidationIssue(path, "unexpected file path; use scripts/, references/, or assets/"))
    if path.startswith("assets/") and is_binary_asset_path(path):
        if not is_valid_base64(content):
            issues.append(ValidationIssue(path, "binary asset content must be valid base64"))
        elif base64_decoded_size(content) > MAX_BINARY_ASSET_BYTES:
            limit = MAX_BINARY_ASSET_BYTES // (1024 * 1024)
            issues.append(ValidationIssue(path, f"binary asset exceeds the {limit}MB limit"))
    return issues


def validate_skill_bundle(files: Mapping[str, str], expected_slug: str | None = None) -> ValidationResult:
    """The strict check: frontmatter schema, name rules, and every path in the bundle.

    A file in a subdirectory other than scripts/, references/ or assets/ is accepted, as
    in the TypeScript; only a stray file at the root is an error.
    """
    skill_md = files.get("SKILL.md")
    if not skill_md:
        return ValidationResult(valid=False, errors=[ValidationIssue("SKILL.md", "SKILL.md is required")])

    parsed = parse_skill_md(skill_md)
    if parsed is None:
        return ValidationResult(
            valid=False,
            errors=[ValidationIssue("SKILL.md", "SKILL.md must contain YAML frontmatter delimited by ---")],
        )

    errors: list[ValidationIssue] = []
    frontmatter: SkillFrontmatter | None = None
    try:
        frontmatter = SkillFrontmatter.model_validate(parsed.frontmatter)
    except ValidationError as exc:
        errors.extend(_frontmatter_issues(exc))

    errors.extend(validate_skill_name(frontmatter.name if frontmatter else "", expected_slug))
    for path, content in files.items():
        if path != "SKILL.md":
            errors.extend(_file_issues(path, content))

    if errors or frontmatter is None:
        return ValidationResult(valid=False, errors=errors)
    return ValidationResult(valid=True, frontmatter=frontmatter, body=parsed.body)


def create_skill_template(slug: str, description: str) -> dict[str, str]:
    """A minimal valid bundle. Serialised, not interpolated: a description containing
    `: ` would otherwise produce frontmatter that fails its own validation."""
    body = f"\n# {slug}\n\nAdd skill instructions here.\n"
    return {"SKILL.md": serialize_skill_md({"name": slug, "description": description}, body)}


def extract_discovery_meta(files: Mapping[str, str]) -> tuple[str, str] | None:
    """(name, description) of a valid bundle, else None."""
    result = validate_skill_bundle(files)
    if not result.valid or result.frontmatter is None:
        return None
    return result.frontmatter.name, result.frontmatter.description


__all__ = [
    "ALLOWED_ROOT_FILES",
    "OPTIONAL_DIRS",
    "SKILL_NAME_RE",
    "ParsedSkillMd",
    "SkillFrontmatter",
    "ValidationIssue",
    "ValidationResult",
    "create_skill_template",
    "extract_discovery_meta",
    "parse_skill_md",
    "serialize_skill_md",
    "split_frontmatter",
    "update_skill_md_frontmatter",
    "validate_skill_bundle",
    "validate_skill_name",
]
