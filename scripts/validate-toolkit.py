#!/usr/bin/env python3
"""Validate the Claude Code toolkit under .claude/.

The toolkit is configuration that runs on every session, so a broken hook or an
invalid settings file is a real outage for whoever works in this repo next. CI
runs this; it needs no dependencies beyond the standard library.

Checks:
  * every hook script parses under `bash -n` and is executable
  * settings.json is valid JSON and every command it references exists
  * subagent frontmatter has a name matching its filename, plus a description
  * skill frontmatter follows the Agent Skills spec (agentskills.io):
    name (<=64 chars, lowercase/digits/hyphens, matching its directory),
    description (<=1024 chars), and no keys outside the six spec fields
  * every skill an agent preloads (`skills:`) exists, and every `references/*.md` a
    skill links to exists
  * every repo path and `make` target the toolkit's Markdown cites still exists -- the
    toolkit is prose about the tree, and prose about a tree rots silently: an audit found
    a migration list eleven revisions behind, a cited `tests/eval/` that was never there,
    and a `.claude/scripts/` directory described in detail that did not exist
  * every route module is mapped to a docs page, in both hooks/lib/surfaces.sh and the
    docs-sync skill's page-map.md, so a new route cannot land with no docs owner
  * every list the prose keeps of something the code defines -- the governance wrapper order,
    the middleware order, the route modules, the spec fields, the session strategies, the
    decider consumers, the CLI commands -- still says what the code says. Each such list sits
    under a `toolkit:enum <key>` marker, and every marker the toolkit is known to carry must
    still be there: removing a marker removes the check
  * every subpackage, and every module of OWNED_MIN_LINES or more, has an owner: a skill whose
    `metadata.covers` claims it, or an UNOWNED entry saying why none does

Usage: validate-toolkit.py [repo-root]   (the root defaults to this script's repository)
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
CLAUDE = ROOT / ".claude"

SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
AGENT_FIELDS = {
    "name",
    "description",
    "tools",
    "disallowedTools",
    "model",
    "permissionMode",
    "maxTurns",
    "skills",
    "mcpServers",
    "hooks",
    "memory",
    "background",
    "effort",
    "isolation",
    "color",
    "initialPrompt",
}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


@functools.cache  # read by more than one check; a missing block is reported once
def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        fail(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
        return {}
    # Stdlib only (CI runs this with a bare python3), so this reads the subset of YAML
    # frontmatter uses: `key: value`, a folded continuation line, a `- item` list, which is
    # kept comma-joined so `skills:` reads the same in either spelling, and one nested level
    # under `metadata:` (`metadata:` → `covers:`), read as `metadata.covers`.
    fields: dict[str, str] = {}
    last = parent = ""
    for line in match.group(1).splitlines():
        key = re.match(r"^([A-Za-z][A-Za-z-]*):\s*(.*)$", line)
        nested = re.match(r"^\s+([A-Za-z][A-Za-z-]*):\s*(.*)$", line)
        if key:
            last = parent = key.group(1)
            fields[last] = key.group(2).strip()
        elif nested and parent == "metadata" and not fields[parent]:
            last = f"{parent}.{nested.group(1)}"
            fields[last] = nested.group(2).strip()
        elif last and (item := re.match(r"^\s+-\s+(.*)$", line)):
            fields[last] = ",".join(filter(None, [fields[last], item.group(1).strip()]))
        elif last and line.startswith((" ", "\t")):
            fields[last] = f"{fields[last]} {line.strip()}".strip()
    return fields


def as_list(value: str) -> list[str]:
    """`[a, b]`, `a, b` or a joined block list, as names."""
    return [item.strip().strip("'\"") for item in value.strip("[]").split(",") if item.strip()]


def check_hooks() -> None:
    hooks = sorted((CLAUDE / "hooks").glob("*.sh"))
    if not hooks:
        fail("no hook scripts found under .claude/hooks/")
    # Libraries are sourced, not run, so they need not be executable -- but a syntax error
    # in one breaks every hook that sources it.
    for hook in [*hooks, *sorted((CLAUDE / "hooks" / "lib").glob("*.sh"))]:
        rel = hook.relative_to(ROOT)
        result = subprocess.run(["bash", "-n", str(hook)], capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"{rel}: shell syntax error\n    {result.stderr.strip()}")
        if hook.parent.name != "lib" and not hook.stat().st_mode & 0o111:
            fail(f"{rel}: not executable (Claude Code cannot run it)")


def check_settings() -> None:
    settings_path = CLAUDE / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f".claude/settings.json: invalid JSON — {exc}")
        return

    referenced: list[str] = []
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                if handler.get("type") == "command":
                    referenced.append(handler["command"])
    status_line = settings.get("statusLine", {})
    if status_line.get("type") == "command":
        referenced.append(status_line["command"])

    for command in referenced:
        # Commands are quoted and rooted at "$CLAUDE_PROJECT_DIR".
        if "/.claude/" not in command:
            continue
        target = CLAUDE / command.split("/.claude/", 1)[1].strip('"')
        if not target.exists():
            fail(f".claude/settings.json references a missing script: {target.relative_to(ROOT)}")
        elif not target.stat().st_mode & 0o111:
            fail(f".claude/settings.json references a non-executable script: {target.relative_to(ROOT)}")


def check_agents() -> None:
    for agent in sorted((CLAUDE / "agents").glob("*.md")):
        rel = agent.relative_to(ROOT)
        fields = frontmatter(agent)
        if not fields:
            continue
        if fields.get("name") != agent.stem:
            fail(f"{rel}: frontmatter name {fields.get('name')!r} != filename {agent.stem!r}")
        if not fields.get("description"):
            fail(f"{rel}: missing description (it is how Claude decides to delegate)")
        unknown = {field.split(".")[0] for field in fields} - AGENT_FIELDS
        if unknown:
            fail(f"{rel}: unknown frontmatter field(s): {', '.join(sorted(unknown))}")
        for skill in as_list(fields.get("skills", "")):
            if not (CLAUDE / "skills" / skill / "SKILL.md").is_file():
                fail(f"{rel}: preloads skill {skill!r}, which has no .claude/skills/{skill}/SKILL.md")


def check_skills() -> None:
    for skill in sorted((CLAUDE / "skills").glob("*/SKILL.md")):
        rel = skill.relative_to(ROOT)
        fields = frontmatter(skill)
        if not fields:
            continue
        name = fields.get("name", "")
        directory = skill.parent.name

        unknown = {field.split(".")[0] for field in fields} - SPEC_FIELDS
        if unknown:
            fail(
                f"{rel}: frontmatter field(s) outside the Agent Skills spec: "
                f"{', '.join(sorted(unknown))} (allowed: {', '.join(sorted(SPEC_FIELDS))})"
            )
        if name != directory:
            fail(f"{rel}: name {name!r} must match its directory {directory!r}")
        if not NAME_RE.match(name):
            fail(f"{rel}: name {name!r} must be lowercase letters, digits, and single hyphens")
        if len(name) > 64:
            fail(f"{rel}: name is {len(name)} characters (spec maximum is 64)")

        description = fields.get("description", "")
        if not 1 <= len(description) <= 1024:
            fail(f"{rel}: description is {len(description)} characters (spec allows 1–1024)")
        if len(fields.get("compatibility", "")) > 500:
            fail(f"{rel}: compatibility exceeds the 500-character maximum")

        for ref in sorted(set(re.findall(r"references/[\w.-]+\.md", skill.read_text(encoding="utf-8")))):
            if not (skill.parent / ref).is_file():
                fail(f"{rel}: links {ref}, which does not exist")


# A token in backticks or a fenced block that starts like one of these is a claim that a
# path exists. Anything templated (`<name>`, `*`, `{a,b}`, `…`, `000N`) is a pattern, not a
# path, and is skipped rather than guessed at.
PATH_PREFIXES = (
    "packages/",
    "apps/",
    "scripts/",
    "tests/",
    "manifests/",
    "migrations/",
    "deploy/",
    "schemas/",
    "fixtures/",
    "docs/",
    "skills/",
    ".github/",
    ".claude/",
)
# Paths in another repository (felix-web) or in gitignored runtime state: not claims about
# this tree.
NOT_THIS_TREE = ("apps/docs/", "apps/chat-ui/", ".claude/worktrees", ".claude/logs")
# House shorthand: `manifests/builder.py` means the harness package's copy, as CLAUDE.md
# writes it. A citation resolves if it exists at the root or under one of these.
SHORTHAND_ROOTS = (
    "",
    "packages/harness/src/felix/",
    "apps/api/src/felix_api/",
    "packages/ai/src/felix_ai/",
    "apps/cli/src/felix_cli/",
    "apps/worker/src/felix_worker/",
    "packages/client/src/felix_client/",
    # `felix/config.py`, `felix_ai/registry.py`: the import-path spelling.
    "packages/harness/src/",
    "packages/ai/src/",
    "packages/client/src/",
    "apps/cli/src/",
    "apps/api/src/",
    "apps/worker/src/",
    # `hooks/lib/surfaces.sh`, `lib/command.sh`: relative to the toolkit.
    ".claude/",
    ".claude/hooks/",
)
# Beyond the prefixes above, any token with a directory and a source-file extension is a
# path claim too. Matching only the prefixes skipped the import-path and toolkit-relative
# spellings -- over a hundred citations, eight in a single skill -- so renaming one of
# those modules left the prose stale behind a green gate.
SOURCE_PATH = re.compile(r"^[\w.-]+(/[\w.-]+)+\.(py|sh|md|yaml|yml|json|toml)(:[^/]*)?$")
TEMPLATED = re.compile(r"[<>*{}$…]|\.\.\.|000N|NNNN|\bX\b")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def cited_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for span in CODE_SPAN.findall(text):
        for token in re.split(r"[\s|,;()\[\]'\"=]+", span.strip("`")):
            tokens.append(token)
    return tokens


def resolve_cited(path: str, doc_dir: Path) -> Path | None:
    """Where a cited path lives: the root, the house shorthand roots, or beside the doc."""
    candidates = [ROOT / base / path for base in SHORTHAND_ROOTS] + [doc_dir / path]
    # `references/x.md` named in prose beside the skill it belongs to ("the code-quality
    # skill's `references/felix-hotspots.md`"): any skill's copy answers the claim.
    if path.startswith("references/"):
        candidates += sorted((CLAUDE / "skills").glob(f"*/{path}"))
    return next((c for c in candidates if c.exists()), None)


def defines(source: Path, symbol: str) -> bool:
    """Defined there, not merely mentioned: main.py imports `create_app` from app.py."""
    name = re.escape(symbol)
    pattern = rf"^\s*(?:async\s+)?(?:def|class)\s+{name}\b|^\s*{name}\s*[:=]"
    return re.search(pattern, source.read_text(encoding="utf-8"), re.M) is not None


def is_path_claim(path: str) -> bool:
    if path.startswith(NOT_THIS_TREE) or TEMPLATED.search(path) or "://" in path:
        return False
    return path.startswith(PATH_PREFIXES) or SOURCE_PATH.match(path) is not None


def check_cited_path(rel: Path, token: str) -> None:
    path = token.removeprefix("./")
    if not is_path_claim(path):
        return
    # `file.py:symbol` / `file.py:123` cite a location in a file. The file is the claim, and
    # a named symbol is a second one: `main.py:create_app` survived a rename that moved
    # `create_app` to app.py.
    symbol_match = re.match(r"^(.*\.py):([A-Za-z_][\w.]*)", path)
    path = re.sub(r":[^/]*$", "", path).rstrip(".:")
    found = resolve_cited(path, (ROOT / rel).parent)
    if found is None:
        fail(f"{rel}: cites `{path}`, which does not exist")
        return
    if symbol_match and found.is_file():
        cited = symbol_match.group(2)
        symbol = cited.split(".")[-1]
        if not defines(found, symbol):
            fail(f"{rel}: cites `{path}:{cited}`, but {symbol!r} is not defined in that file")


def check_citations() -> None:
    makefile = ROOT / "Makefile"
    text_of = makefile.read_text(encoding="utf-8") if makefile.is_file() else ""
    targets = set(re.findall(r"^([A-Za-z0-9][\w.-]*):", text_of, re.M))
    for doc in sorted(CLAUDE.rglob("*.md")):
        if {"worktrees", "logs"} & set(doc.relative_to(CLAUDE).parts):
            continue
        rel = doc.relative_to(ROOT)
        text = doc.read_text(encoding="utf-8")
        for token in sorted(set(cited_tokens(text))):
            check_cited_path(rel, token)
        for span in CODE_SPAN.findall(text):
            for target in re.findall(r"(?<![\w-])make ([a-z][a-z0-9-]*)", span):
                if targets and target not in targets:
                    fail(f"{rel}: cites `make {target}`, which is not a Makefile target")


def route_row(stem: str) -> str:
    name = re.escape(stem)
    return rf"routes/{name}\.py|routes/\{{[^}}]*\b{name}\b[^}}]*\}}\.py|`{name}\.py`"


def check_route_docs_map() -> None:
    routes_dir = ROOT / "apps/api/src/felix_api/routes"
    surfaces = CLAUDE / "hooks/lib/surfaces.sh"
    page_map = CLAUDE / "skills/docs-sync/references/page-map.md"
    if not (routes_dir.is_dir() and surfaces.is_file() and page_map.is_file()):
        return
    mapped = surfaces.read_text(encoding="utf-8")
    table = page_map.read_text(encoding="utf-8")
    for module in sorted(routes_dir.glob("*.py")):
        if module.name == "__init__.py":
            continue
        if f"routes/{module.name}" not in mapped:
            fail(f".claude/hooks/lib/surfaces.sh: route module {module.name} maps to no docs page")
        # Anchored on the route row's own spelling -- `routes/<name>.py` or a member of a
        # `routes/{a,b}.py` group -- so a stem that merely appears as a word in another row's
        # description (`jobs`, `files`, `memory`) does not count as mapped.
        if not module.name.startswith("_") and not re.search(route_row(module.stem), table):
            fail(f"{page_map.relative_to(ROOT)}: route module {module.name} is missing from the table")


# --- enumerations: prose lists of things the code defines ------------------------------------
#
# A path citation survives a wrapper added to the governance stack: the file it names is still
# there. That is how six copies of the wrapper order fell one wrapper behind, and how CLAUDE.md
# went on listing four session strategies after a fifth existed. Each list that restates code
# sits under a marker -- `<!-- toolkit:enum KEY -->` in Markdown, `# toolkit:enum KEY` in a
# hook -- and is compared here with the definition it restates. A marker stands alone on its
# line, so prose that quotes one in backticks is not one. An inline list that runs on into
# other prose ends at `<!-- /toolkit:enum -->`, so names mentioned after it are not read as
# members of it.

ENUM_MARKER = re.compile(
    r"^\s*<!--\s*toolkit:enum\s+([\w-]+)\s*-->\s*$|^\s*#\s*toolkit:enum\s+([\w-]+)\b.*$", re.M
)
ENUM_END = "<!-- /toolkit:enum -->"
ARROW = re.compile(r"→|->")

# Where each list lives. A marker that disappears is a check that disappears, so its absence
# fails as loudly as a wrong list.
EXPECTED_MARKERS: dict[str, set[str]] = {
    "CLAUDE.md": {"wrapper-order", "middleware-order", "session-strategies", "cli-commands"},
    "README.md": {"session-strategies"},
    ".claude/rules/felix-invariants.md": {"wrapper-order"},
    ".claude/hooks/compact-reminder.sh": {"wrapper-order"},
    ".claude/skills/code-quality/references/felix-hotspots.md": {"wrapper-order"},
    ".claude/skills/governance-pipeline/SKILL.md": {"wrapper-order"},
    ".claude/skills/api-surface/SKILL.md": {"middleware-order", "route-modules"},
    ".claude/skills/manifest-authoring/SKILL.md": {"spec-fields"},
    ".claude/skills/manifest-authoring/references/spec-fields.md": {
        "session-strategies",
        "decider-consumers",
    },
    ".claude/skills/model-layer/SKILL.md": {"decider-consumers"},
    ".claude/agents/felix-manifest-architect.md": {"session-strategies"},
    "manifests/self/skills/felix-architecture/SKILL.md": {"wrapper-order"},
}


class SourceUnreadable(Exception):
    """A definition the check compares against could not be read; reported, never a traceback."""


def _module(rel: str) -> ast.Module | None:
    path = ROOT / rel
    if not path.is_file():
        return None
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        # CI runs this with the runner's python3, which can be older than the syntax the repo
        # uses (`except A, B:` is 3.14). One unparseable file must not take every check with it.
        raise SourceUnreadable(
            f"{rel} does not parse under Python {sys.version.split()[0]}: {exc.msg}"
        ) from exc


def _assigned(tree: ast.Module | None, name: str) -> ast.expr | None:
    for node in ast.walk(tree) if tree else ():
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    return None


def _literal(value: ast.expr, where: str) -> object:
    try:
        return ast.literal_eval(value)
    except ValueError as exc:
        raise SourceUnreadable(f"{where} is no longer a literal this check can read") from exc


def _words(name: str) -> str:
    """`apply_secret_masking` -> `secret masking`; `RateLimitMiddleware` -> `rate limit`."""
    name = name.removeprefix("apply_").removesuffix("Middleware")
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ")
    return name.lower().strip()


def code_wrapper_order() -> list[str]:
    value = _assigned(_module("tests/unit/test_invariants.py"), "EXPECTED_WRAPPER_ORDER")
    if value is None:
        return []
    return [_words(name) for name in _literal(value, "EXPECTED_WRAPPER_ORDER")]  # type: ignore[union-attr]


def code_middleware_order() -> list[str]:
    tree = _module("apps/api/src/felix_api/app.py")
    registered = [
        node.args[0].id
        for node in (ast.walk(tree) if tree else ())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_middleware"
        and node.args
        and isinstance(node.args[0], ast.Name)
    ]
    # `add_middleware` inserts at the front, so the runtime order is the registration reversed.
    return [_words(name) for name in reversed(registered)]


def code_route_modules() -> set[str]:
    routes = ROOT / "apps/api/src/felix_api/routes"
    return {module.stem for module in routes.glob("*.py") if module.name != "__init__.py"}


def _schema() -> dict:
    path = ROOT / "schemas/manifest.schema.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def code_spec_fields() -> set[str]:
    return set(_schema().get("$defs", {}).get("Spec", {}).get("properties", {}))


def code_decider_consumers() -> set[str]:
    """Every manifest path of a boolean `decider` flag -- each one a consumer that opts in."""
    defs = _schema().get("$defs", {})

    def resolve(node: dict) -> dict:
        if "$ref" in node:
            return defs.get(node["$ref"].rsplit("/", 1)[-1], {})
        for key in ("anyOf", "allOf", "oneOf"):
            for option in node.get(key, []):
                found = resolve(option)
                if found.get("properties") or found.get("items"):
                    return found
        return node

    def is_boolean(node: dict) -> bool:
        # `decider: bool | None` is `anyOf: [boolean, null]` -- a consumer all the same.
        options = [node, *node.get("anyOf", []), *node.get("oneOf", [])]
        return any(option.get("type") == "boolean" for option in options)

    found: set[str] = set()

    def walk(node: dict, path: str, depth: int) -> None:
        node = resolve(node)
        if depth > 8:
            return
        if "items" in node:
            walk(node["items"], f"{path}[]", depth + 1)
            return
        for key, value in node.get("properties", {}).items():
            if key == "decider" and is_boolean(value):
                found.add(f"{path}.{key}".lstrip("."))
            else:
                walk(value, f"{path}.{key}", depth + 1)

    if "Spec" in defs:
        walk(defs["Spec"], "", 0)
    return found


def code_session_strategies() -> set[str]:
    value = _assigned(
        _module("packages/harness/src/felix/session/strategies.py"), "_BUILTIN_STRATEGY_PREFIXES"
    )
    if value is None:
        return set()
    if isinstance(value, ast.Call):  # frozenset({...})
        if not value.args:
            return set()
        value = value.args[0]
    return set(_literal(value, "_BUILTIN_STRATEGY_PREFIXES"))  # type: ignore[arg-type]


def code_cli_commands() -> set[str]:
    def on_app(call: ast.expr, attr: str) -> bool:
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == attr
            # Top-level only: `sessions_app.command("backfill-previews")` is a subcommand.
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "app"
        )

    tree = _module("apps/cli/src/felix_cli/main.py")
    names: set[str] = set()
    for node in ast.walk(tree) if tree else ():
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if on_app(decorator, "command"):
                    first = decorator.args[0] if decorator.args else None  # type: ignore[attr-defined]
                    # A bare `@app.command()` is named after the function, as Typer names it.
                    named = first.value if isinstance(first, ast.Constant) else None
                    names.add(named or node.name.replace("_", "-"))
        elif on_app(node, "add_typer"):
            for kw in node.keywords:  # type: ignore[attr-defined]
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
        # `app.command("chat")(chat)`: a command defined in its own module, registered here.
        elif isinstance(node, ast.Call) and on_app(node.func, "command"):
            first = node.func.args[0] if node.func.args else None  # type: ignore[attr-defined]
            if isinstance(first, ast.Constant):
                names.add(first.value)
    return names


def enum_block(text: str, end_of_marker: int, shell: bool) -> str:
    """The list a marker governs: the next line in a hook; in Markdown the next fenced block,
    else the next paragraph or table, which a blank line or the start of another list item
    ends -- and an explicit end marker ends sooner."""
    lines = text[end_of_marker:].lstrip("\n").splitlines()
    if shell:
        return lines[0] if lines else ""
    if lines and lines[0].strip().startswith("```"):
        body = []
        for line in lines[1:]:
            if line.strip().startswith("```"):
                break
            body.append(line)
        return "\n".join(body)
    block: list[str] = []
    for i, line in enumerate(lines):
        if not line.strip() or (i and re.match(r"^\s*(?:[-*]|\d+\.)\s", line)):
            break
        block.append(line)
    return "\n".join(block).split(ENUM_END, 1)[0]


def _label(label: str) -> str:
    """A label as a pattern: `request id` also matches `request-id`, `RequestId`, `request_id`."""
    return r"[\s_-]*".join(map(re.escape, label.split()))


def check_order(rel: str, key: str, block: str, expected: list[str]) -> None:
    """Every element, in order, and no more -- each one at its own arrow.

    Splitting on the arrows and reading each element where it stands, rather than searching the
    paragraph for each label, is the point: a replaced last element went unreported while the
    word survived later in the same paragraph (`auth/mgmt.py` stood in for `AuthMiddleware`)."""
    order = " → ".join(expected)
    segments = [re.sub(r"\s+", " ", s).strip().lower() for s in ARROW.split(block)]
    if len(segments) != len(expected):
        fail(f"{rel}: the {key} list has {len(segments)} elements; the code has {len(expected)} ({order})")
        return
    edge = r"[\s`*_]*"
    for i, (segment, label) in enumerate(zip(segments, expected, strict=True)):
        # The first element ends its segment (prose leads into it); every other one opens its
        # segment (prose may trail the last, and a gloss like "guardrails (PII)" may follow).
        pattern = rf"\b{_label(label)}{edge}$" if i == 0 else rf"^{edge}{_label(label)}"
        if not re.search(pattern, segment):
            fail(f"{rel}: the {key} list has {segment[-40:]!r} where the code has {label!r} ({order})")
            return


def check_members(rel: str, key: str, named: set[str], expected: set[str]) -> None:
    for missing in sorted(expected - named):
        fail(f"{rel}: the {key} list does not name {missing!r}")
    for extra in sorted(named - expected):
        fail(f"{rel}: the {key} list names {extra!r}, which the code does not define")


def _ticked(block: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", block)


def _is_table(block: str) -> bool:
    return block.lstrip().startswith("|")


def _first_cells(block: str) -> str:
    return "\n".join(row.split("|")[1] for row in block.splitlines() if row.count("|") >= 2)


def check_enum(rel: str, key: str, block: str) -> None:
    if key == "wrapper-order":
        check_order(rel, key, block, code_wrapper_order())
    elif key == "middleware-order":
        check_order(rel, key, block, code_middleware_order())
    elif key == "route-modules":
        named = set(re.findall(r"(?<![\w/.])(\w+)\.py\b", block))
        check_members(rel, key, named, code_route_modules())
    elif key == "spec-fields":
        named = {re.split(r"[.:\s\[]", token)[0] for token in _ticked(_first_cells(block))}
        check_members(rel, key, named, code_spec_fields())
    elif key == "session-strategies":
        # A table lists them in its first column; an inline list is everything ticked up to the
        # end marker, so a name the prose mentions again afterwards cannot stand in for one the
        # list dropped.
        cells = _first_cells(block) if _is_table(block) else block
        named = {re.split(r"[:\[]", token)[0] for token in _ticked(cells)}
        check_members(rel, key, named, code_session_strategies())
    elif key == "decider-consumers":
        # `spec.decider` is the decider itself, not a consumer of it.
        named = {path for path in re.findall(r"[\w\[\].]+\.decider\b", block) if not path.startswith("spec.")}
        check_members(rel, key, named, code_decider_consumers())
    elif key == "cli-commands":
        piped = next((token for token in _ticked(block) if "|" in token), "")
        named = {name.strip() for name in piped.split("|") if name.strip()}
        check_members(rel, key, named, code_cli_commands())
    else:
        fail(f"{rel}: unknown toolkit:enum key {key!r}")


def enum_sources() -> list[Path]:
    sources = [
        ROOT / "CLAUDE.md",
        ROOT / "README.md",
        *sorted((ROOT / "skills").glob("*/SKILL.md")),
        *sorted((ROOT / "manifests" / "self" / "skills").glob("*/SKILL.md")),
    ]
    # Pruned during the walk: `.claude/worktrees/` holds whole checkouts, venvs included, and
    # filtering them out afterwards made the main checkout's run three times slower.
    for directory, subdirs, files in os.walk(CLAUDE):
        subdirs[:] = sorted(d for d in subdirs if d not in {"worktrees", "logs"})
        sources += [Path(directory) / f for f in sorted(files) if f.endswith((".md", ".sh"))]
    return [p for p in sources if p.is_file()]


def check_enumerations() -> None:
    seen: dict[str, set[str]] = {}
    for source in enum_sources():
        rel = str(source.relative_to(ROOT))
        text = source.read_text(encoding="utf-8")
        for marker in ENUM_MARKER.finditer(text):
            key = marker.group(1) or marker.group(2)
            seen.setdefault(rel, set()).add(key)
            try:
                check_enum(rel, key, enum_block(text, marker.end(), source.suffix == ".sh"))
            except SourceUnreadable as exc:
                fail(f"{rel}: cannot check the {key} list -- {exc}")
    for rel, keys in EXPECTED_MARKERS.items():
        if not (ROOT / rel).is_file():
            continue
        for key in sorted(keys - seen.get(rel, set())):
            fail(f"{rel}: lost its `toolkit:enum {key}` marker -- the list under it is no longer checked")


# --- ownership -------------------------------------------------------------------------
#
# The checks above keep what the toolkit says true; nothing above notices what it never
# says. `felix/tools/`, `felix/skills/` and `felix/durability/` -- over 20k lines between
# them -- grew for months with no skill describing them, so an agent working there had
# only the code. Every subpackage, and every module of OWNED_MIN_LINES or more, in the
# packages below must be claimed by a skill's `metadata.covers` or listed in UNOWNED with
# the reason nobody owns it yet. A new package then fails until someone decides.

PACKAGE_ROOTS = {
    "felix": "packages/harness/src/felix",
    "felix_ai": "packages/ai/src/felix_ai",
    "felix_client": "packages/client/src/felix_client",
    "felix_api": "apps/api/src/felix_api",
    "felix_cli": "apps/cli/src/felix_cli",
    "felix_worker": "apps/worker/src/felix_worker",
}
OWNED_MIN_LINES = 300
# Roots a skill may not claim whole. The others are claimed whole today (api-surface owns
# `felix_api/`), so the check's force is inside the harness, where no one skill can own it all.
UNCLAIMABLE_WHOLE = {"felix"}
# Code no skill describes yet, and why that is acceptable for now. Like `KNOWN_OPEN` in
# tests/unit/test_ordering_rule.py this only shrinks: an entry a skill now covers, or that
# no longer exists, fails until it is removed.
UNOWNED: dict[str, str] = {}


def ownership_units() -> list[str]:
    """Every subpackage and every module over the size floor, in import-path spelling."""
    units: list[str] = []
    for name, rel in PACKAGE_ROOTS.items():
        root = ROOT / rel
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and any("__pycache__" not in p.parts for p in entry.rglob("*.py")):
                units.append(f"{name}/{entry.name}/")
            elif entry.suffix == ".py" and entry.name != "__init__.py":
                with entry.open(encoding="utf-8") as handle:
                    if sum(1 for _ in handle) >= OWNED_MIN_LINES:
                        units.append(f"{name}/{entry.name}")
    return units


def owned_path(entry: str) -> Path | None:
    """`felix/tools/` → the directory it names, or None when it names nothing.

    The smaller packages may be claimed whole (`felix_api/`); the harness may not, since one
    bare `felix` entry would turn the check off where it has force."""
    head, _, tail = entry.partition("/")
    if head not in PACKAGE_ROOTS or (head in UNCLAIMABLE_WHOLE and not tail.strip("/")):
        return None
    path = ROOT / PACKAGE_ROOTS[head] / tail
    return path if path.exists() else None


def skill_covers() -> dict[str, list[str]]:
    covers: dict[str, list[str]] = {}
    for skill in sorted((CLAUDE / "skills").glob("*/SKILL.md")):
        fields = frontmatter(skill)
        for entry in as_list(fields.get("metadata.covers", "")):
            path = owned_path(entry)
            if path is None:
                fail(
                    f"{skill.relative_to(ROOT)}: covers {entry!r}, which names nothing under "
                    f"{', '.join(f'{k}/' for k in PACKAGE_ROOTS)}"
                )
                continue
            entry = entry if path.is_file() or entry.endswith("/") else f"{entry}/"
            covers.setdefault(entry, []).append(skill.parent.name)
    return covers


def check_ownership() -> None:
    covers = skill_covers()

    def owners(unit: str) -> list[str]:
        return sorted({s for entry, skills in covers.items() if unit.startswith(entry) for s in skills})

    units = ownership_units()
    for unit in units:
        if not owners(unit) and unit not in UNOWNED:
            fail(
                f"{unit} has no owner: add it to the `metadata.covers` of the skill that describes it, "
                f"or to UNOWNED in scripts/validate-toolkit.py with the reason none does"
            )
    for unit in sorted(UNOWNED):
        if unit not in units:
            fail(
                f"UNOWNED lists {unit}, which is no longer a subpackage or a module of "
                f"{OWNED_MIN_LINES}+ lines -- remove it"
            )
        elif owned_by := owners(unit):
            fail(f"UNOWNED lists {unit}, which {', '.join(owned_by)} now covers -- remove it")


def main() -> int:
    if not CLAUDE.is_dir():
        print("no .claude/ directory — nothing to validate")
        return 0
    check_hooks()
    check_settings()
    check_agents()
    check_skills()
    check_citations()
    check_route_docs_map()
    check_enumerations()
    check_ownership()

    if errors:
        print(f"Claude Code toolkit: {len(errors)} problem(s)\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    hooks = len(list((CLAUDE / "hooks").glob("*.sh")))
    agents = len(list((CLAUDE / "agents").glob("*.md")))
    skills = len(list((CLAUDE / "skills").glob("*/SKILL.md")))
    print(f"Claude Code toolkit OK — {hooks} hooks, {agents} agents, {skills} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
