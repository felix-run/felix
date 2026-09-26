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

Usage: validate-toolkit.py [repo-root]   (the root defaults to this script's repository)
"""

from __future__ import annotations

import json
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


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        fail(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
        return {}
    # Stdlib only (CI runs this with a bare python3), so this reads the subset of YAML
    # frontmatter uses: `key: value`, a folded continuation line, and a `- item` list,
    # which is kept comma-joined so `skills:` reads the same in either spelling.
    fields: dict[str, str] = {}
    last = ""
    for line in match.group(1).splitlines():
        key = re.match(r"^([A-Za-z][A-Za-z-]*):\s*(.*)$", line)
        if key:
            last = key.group(1)
            fields[last] = key.group(2).strip()
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
        unknown = set(fields) - AGENT_FIELDS
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

        unknown = set(fields) - SPEC_FIELDS
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
    "clients/",
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
    "packages/cli/src/felix_cli/",
    "apps/worker/src/felix_worker/",
    # `felix/config.py`, `felix_ai/registry.py`: the import-path spelling.
    "packages/harness/src/",
    "packages/ai/src/",
    "packages/cli/src/",
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
