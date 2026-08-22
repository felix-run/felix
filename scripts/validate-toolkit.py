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
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key = re.match(r"^([A-Za-z][A-Za-z-]*):\s*(.*)$", line)
        if key:
            fields[key.group(1)] = key.group(2).strip()
    return fields


def check_hooks() -> None:
    hooks = sorted((CLAUDE / "hooks").glob("*.sh"))
    if not hooks:
        fail("no hook scripts found under .claude/hooks/")
    for hook in hooks:
        rel = hook.relative_to(ROOT)
        result = subprocess.run(["bash", "-n", str(hook)], capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"{rel}: shell syntax error\n    {result.stderr.strip()}")
        if not hook.stat().st_mode & 0o111:
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


def main() -> int:
    if not CLAUDE.is_dir():
        print("no .claude/ directory — nothing to validate")
        return 0
    check_hooks()
    check_settings()
    check_agents()
    check_skills()

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
