"""The SKILL.md format (agentskills.io): YAML frontmatter, bundle layout, templates.

`parse_skill_md` splits a SKILL.md into its frontmatter and body without judging either;
`validate_skill_bundle` is the strict check an authoring path applies before a bundle is
saved. The catalog loader (`skills/loader.py`) reads leniently on top of the parser and
never applies the strict check — a skill already on disk keeps loading.

Pure: no I/O and no settings. Rules a permissive validator would not apply:

- Bundle paths are an allowlist: every segment is `[A-Za-z0-9._-]{1,128}`, and the first is
  `scripts`, `references`, `assets` or `evals` unless the path is exactly `plugin.json`. A
  file in any other subdirectory, or a root file named `scripts`, is refused.
- A bundle is capped in SKILL.md size, file count and total size, and frontmatter in size
  and nesting depth. YAML anchors and aliases are refused, and an unknown frontmatter key
  must hold a scalar or a flat list or map of scalars.
- A fence line may carry trailing spaces or tabs.
- Lengths are counted in code points, not UTF-16 units.
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
from ruamel.yaml.composer import Composer, ComposerError
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.emitter import Emitter
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import AliasEvent

from felix.skills.binary import (
    MAX_BINARY_ASSET_BYTES,
    base64_decoded_size,
    is_binary_asset_path,
    is_valid_base64,
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_NAME_LENGTH_MESSAGE = "name must be 1-64 characters"
_NAME_CHARS_MESSAGE = (
    "name may only contain lowercase letters, numbers, and hyphens; no leading/trailing/consecutive hyphens"
)

BUNDLE_DIRS = ("scripts", "references", "assets", "evals")
ALLOWED_ROOT_FILES = ("plugin.json",)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")

FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", re.DOTALL)

# Bounds on what a parse or a validation will take on. The frontmatter cap applies before
# YAML sees the text; the depth cap stops a `[[[[…]]]]` bomb inside the composer.
MAX_FRONTMATTER_CHARS = 64 * 1024
MAX_FRONTMATTER_DEPTH = 32
MAX_SKILL_MD_CHARS = 256 * 1024
MAX_BUNDLE_FILES = 200
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

# Known keys first, in this order; anything else after, in the order given.
_KEY_ORDER = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")


class _Constructor(SafeConstructor):
    """Safe loading, with timestamps left as strings.

    The YAML 1.2 core schema the TypeScript `yaml` package reads has no timestamp type, so
    `updated: 2026-01-01` in `metadata` is a string there and a valid frontmatter value.
    ruamel would hand back a `date` and fail the `dict[str, str]` check on the same file.
    """


_Constructor.add_constructor("tag:yaml.org,2002:timestamp", _Constructor.construct_yaml_str)


class _TextConstructor(_Constructor):
    """Every scalar as its source text — the catalog's read path, where a value is a string.

    `version: 1.10` is the float 1.1 to YAML and the version "1.10" to the author.
    """


for _tag in ("null", "bool", "int", "float"):
    _TextConstructor.add_constructor(f"tag:yaml.org,2002:{_tag}", _TextConstructor.construct_yaml_str)


class _Composer(Composer):
    """No anchors or aliases. Frontmatter has no use for them, and an alias is how a few
    lines of YAML expand into a very large document."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.parser.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ComposerError(None, None, "anchors and aliases are not allowed", event.start_mark)
        return super().compose_node(parent, index)


def _loader(constructor: type[SafeConstructor]) -> YAML:
    """A fresh loader per parse. A ruamel `YAML` keeps composer state between loads — after
    a `MaxDepthExceededError` its depth counter never unwinds, so every later load on the
    same instance fails — and it is not safe to share across the threads the catalog
    loads in."""
    yaml = YAML(typ="safe", pure=True)
    yaml.Constructor = constructor
    yaml.Composer = _Composer
    yaml.max_depth = MAX_FRONTMATTER_DEPTH
    return yaml


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


def _name_problem(name: str) -> str | None:
    """The one skill-name rule, as the message for whatever it breaks first."""
    if not 1 <= len(name) <= 64:
        return _NAME_LENGTH_MESSAGE
    if not SKILL_NAME_RE.match(name):
        return _NAME_CHARS_MESSAGE
    return None


def is_valid_skill_name(name: str) -> bool:
    return _name_problem(name) is None


def _skill_name(value: str) -> str:
    problem = _name_problem(value)
    if problem:
        raise ValueError(problem)
    return value


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _flat_value(value: Any) -> Any:
    if isinstance(value, list):
        flat = all(_is_scalar(v) for v in value)
    elif isinstance(value, dict):
        flat = all(isinstance(k, str) and _is_scalar(v) for k, v in value.items())
    else:
        flat = _is_scalar(value)
    if not flat:
        raise ValueError("must be a scalar, or a flat list or map of scalars")
    return value


class SkillFrontmatter(BaseModel):
    """The agentskills.io frontmatter. Extra keys are kept, so a read-modify-write of a
    SKILL.md does not drop a field this model does not know about — but only scalars and
    flat lists or maps of them."""

    model_config = ConfigDict(extra="allow", validate_by_name=True, validate_by_alias=True)

    name: Annotated[str, AfterValidator(_skill_name)]
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    compatibility: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str] | None = None
    allowed_tools: str | None = Field(default=None, alias="allowed-tools")

    # Typing the extras validates each unknown key's value under its own key.
    __pydantic_extra__: dict[str, Annotated[Any, AfterValidator(_flat_value)]] = Field(init=False)


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
    frontmatter: SkillFrontmatter | None = None
    body: str | None = None
    errors: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.frontmatter is not None and not self.errors


def split_frontmatter(content: str) -> tuple[str, str] | None:
    """(yaml text, body) between the `---` fences, or None when there are none."""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_skill_md(content: str, *, scalars_as_text: bool = False) -> ParsedSkillMd | None:
    """Split a SKILL.md; None when the fences are missing or the YAML is refused.

    Refused: YAML that does not parse, frontmatter over `MAX_FRONTMATTER_CHARS`, nesting past
    `MAX_FRONTMATTER_DEPTH`, and any anchor or alias. ``scalars_as_text`` keeps every scalar
    as the text it was written as (the catalog's read path); the default types them as the
    YAML 1.2 core schema does, which is what the strict validation checks.
    """
    parts = split_frontmatter(content)
    if parts is None:
        return None
    yaml_text, body = parts
    if len(yaml_text) > MAX_FRONTMATTER_CHARS:
        return None
    try:
        frontmatter = _loader(_TextConstructor if scalars_as_text else _Constructor).load(yaml_text)
    except YAMLError, RecursionError:
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
    """At most one issue for the name's shape, plus one if it differs from ``slug``."""
    errors: list[ValidationIssue] = []
    problem = _name_problem(name)
    if problem:
        errors.append(ValidationIssue("name", problem))
    if slug and name != slug:
        errors.append(ValidationIssue("name", f'name must match skill slug "{slug}"'))
    return errors


def _frontmatter_issues(exc: ValidationError) -> list[ValidationIssue]:
    issues = []
    for err in exc.errors():
        message = str(err["msg"]).removeprefix("Value error, ")
        issues.append(ValidationIssue(f"frontmatter.{'.'.join(str(p) for p in err['loc'])}", message))
    return issues


_ESCAPE_MESSAGE = "file path must be relative and must not contain '..' or a leading slash"


def _path_issue(path: str) -> str | None:
    """Why ``path`` may not be in a bundle, or None. An allowlist: a bundle entry is
    eventually written under some root, and anything it does not name is refused."""
    if path in ALLOWED_ROOT_FILES:
        return None
    segments = path.split("/")
    if "\\" in path or any(s in {"", ".", ".."} for s in segments):
        return _ESCAPE_MESSAGE
    if not all(_SEGMENT_RE.match(s) for s in segments):
        return "each path segment must be 1-128 characters of A-Z, a-z, 0-9, '.', '_' or '-'"
    if len(segments) < 2 or segments[0] not in BUNDLE_DIRS:
        return "unexpected file path; use scripts/, references/, assets/, or evals/"
    if segments[-1] == "SKILL.md":
        return "SKILL.md belongs only at the bundle root"
    return None


def bundle_path_issue(path: str) -> str | None:
    """Why ``path`` may not name a file in a bundle, or None — the same allowlist a save
    applies, for a reader that is handed a path rather than a bundle."""
    return _path_issue(path)


def _file_issues(path: str, content: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if is_binary_asset_path(path) and not path.startswith("assets/"):
        issues.append(ValidationIssue(path, "binary assets (images, PDFs, archives) must live under assets/"))
    problem = _path_issue(path)
    if problem:
        issues.append(ValidationIssue(path, problem))
        return issues
    if path.startswith("assets/") and is_binary_asset_path(path):
        if not is_valid_base64(content):
            issues.append(ValidationIssue(path, "binary asset content must be valid base64"))
        elif base64_decoded_size(content) > MAX_BINARY_ASSET_BYTES:
            limit = MAX_BINARY_ASSET_BYTES // (1024 * 1024)
            issues.append(ValidationIssue(path, f"binary asset exceeds the {limit}MB limit"))
    return issues


def _size_issues(files: Mapping[str, str], skill_md: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if len(files) > MAX_BUNDLE_FILES:
        issues.append(ValidationIssue("bundle", f"a bundle may hold at most {MAX_BUNDLE_FILES} files"))
    if sum(len(c.encode("utf-8")) for c in files.values()) > MAX_BUNDLE_BYTES:
        issues.append(
            ValidationIssue("bundle", f"a bundle may total at most {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB")
        )
    if len(skill_md) > MAX_SKILL_MD_CHARS:
        issues.append(
            ValidationIssue("SKILL.md", f"SKILL.md may be at most {MAX_SKILL_MD_CHARS // 1024} KiB")
        )
    return issues


def validate_skill_bundle(files: Mapping[str, str], expected_slug: str | None = None) -> ValidationResult:
    """The strict check: size caps, frontmatter schema, name rules, and every path."""
    skill_md = files.get("SKILL.md")
    if not skill_md:
        return ValidationResult(errors=[ValidationIssue("SKILL.md", "SKILL.md is required")])
    oversized = _size_issues(files, skill_md)
    if oversized:
        return ValidationResult(errors=oversized)

    parsed = parse_skill_md(skill_md)
    if parsed is None:
        return ValidationResult(
            errors=[
                ValidationIssue(
                    "SKILL.md",
                    "SKILL.md must contain YAML frontmatter delimited by --- "
                    "(within the size and nesting limits, with no anchors or aliases)",
                )
            ]
        )

    errors: list[ValidationIssue] = []
    frontmatter: SkillFrontmatter | None = None
    try:
        frontmatter = SkillFrontmatter.model_validate(parsed.frontmatter)
    except ValidationError as exc:
        errors.extend(_frontmatter_issues(exc))

    # The name's shape is the model's to report; the slug is checked here, against the
    # name as written even when the model refused the rest of the frontmatter.
    raw = parsed.frontmatter if isinstance(parsed.frontmatter, dict) else {}
    name = frontmatter.name if frontmatter else raw.get("name")
    if expected_slug and name != expected_slug:
        errors.append(ValidationIssue("frontmatter.name", f'name must match skill slug "{expected_slug}"'))

    for path, content in files.items():
        if path != "SKILL.md":
            errors.extend(_file_issues(path, content))

    if errors:
        return ValidationResult(errors=errors)
    return ValidationResult(frontmatter=frontmatter, body=parsed.body)


def create_skill_template(slug: str, description: str) -> dict[str, str]:
    """A minimal valid bundle. Serialised, not interpolated: a description containing
    `: ` would otherwise produce frontmatter that fails its own validation."""
    body = f"\n# {slug}\n\nAdd skill instructions here.\n"
    return {"SKILL.md": serialize_skill_md({"name": slug, "description": description}, body)}


def extract_discovery_meta(files: Mapping[str, str]) -> tuple[str, str] | None:
    """(name, description) of a valid bundle, else None."""
    frontmatter = validate_skill_bundle(files).frontmatter
    return (frontmatter.name, frontmatter.description) if frontmatter else None


__all__ = [
    "ALLOWED_ROOT_FILES",
    "BUNDLE_DIRS",
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_FILES",
    "MAX_FRONTMATTER_CHARS",
    "MAX_FRONTMATTER_DEPTH",
    "MAX_SKILL_MD_CHARS",
    "SKILL_NAME_RE",
    "ParsedSkillMd",
    "SkillFrontmatter",
    "ValidationIssue",
    "ValidationResult",
    "bundle_path_issue",
    "create_skill_template",
    "extract_discovery_meta",
    "is_valid_skill_name",
    "parse_skill_md",
    "serialize_skill_md",
    "split_frontmatter",
    "update_skill_md_frontmatter",
    "validate_skill_bundle",
    "validate_skill_name",
]
