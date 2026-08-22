# Felix Claude Code toolkit

Project-scoped configuration for [Claude Code](https://code.claude.com/docs/en/overview), tuned to
this repo: the Felix agents harness (Python 3.14, uv workspace) and its companion docs site in the
separate **felix-web** repo.

```
.claude/
├── settings.json     hook registration, permission allow/ask/deny, status line
├── agents/           9 subagents (delegated, isolated context)
├── skills/           11 Agent Skills (agentskills.io format, loaded on demand)
├── hooks/            13 lifecycle hooks (deterministic enforcement)
├── rules/            always-loaded invariants
└── logs/             subagent audit trail (gitignored)
```

## Subagents — `.claude/agents/*.md`

Delegate with the Agent tool or by name. Each runs in its own context and reports back.

| Agent | Use for |
|---|---|
| `felix-engineer` | Feature/fix implementation across the harness, API, worker, CLI |
| `felix-postgres` | Schema, Alembic migrations, RLS, pgvector, session log, stores |
| `felix-devops` | Docker/Compose, Helm, AWS/GCP, CI, lean-image and memory budgets |
| `felix-code-reviewer` | Correctness + invariant review of a diff, branch, or PR |
| `felix-security-reviewer` | Tenant isolation, auth/scopes, screening, secrets, SSRF, sandboxes (opus) |
| `felix-manifest-architect` | `felix/v1` manifests and schema↔builder wiring |
| `felix-test-engineer` | Tests under the `memory://` path, fixtures, eval |
| `felix-dx-maintainer` | Makefile, CLI, pre-commit, and this toolkit |
| `felix-docs-syncer` | In-repo docs + the public Starlight MDX in felix-web |

## Skills — `.claude/skills/*/SKILL.md`

Claude loads a skill when its description matches the task; you can invoke one directly with
`/<name>`. Frontmatter is restricted to the [Agent Skills](https://agentskills.io) spec fields
(`name`, `description`, and optionally `license`, `compatibility`, `metadata`, `allowed-tools`), so
these skills are portable to any skills-compatible agent.

| Skill | Covers |
|---|---|
| `felix-dev-loop` | Install, run, and the exact gates CI enforces; the `memory://` test path |
| `manifest-authoring` | Writing `felix/v1` manifests; adding a spec field (+ `references/spec-fields.md`) |
| `governance-pipeline` | The compile pipeline and tool wrapper stack; adding a control |
| `api-surface` | Adding/changing REST, `/v1`, A2A, MCP, and management endpoints |
| `postgres-migrations` | Alembic revisions, RLS, pgvector, in-memory twins |
| `plugin-seam` | Optional features, extras, and the lean-default rule |
| `security-review` | Threat model and control map (+ `references/checklist.md`) |
| `docs-sync` | Surface → doc page mapping across both repos (+ `references/page-map.md`) |
| `deploy-runbook` | Compose overlays, Helm, production configuration checklist |
| `python-conventions` | Style, Protocols, lazy imports, async, ruff/ty exemptions |
| `branch-pr-workflow` | Branch naming, feature-scoped PRs, commit style, PR gates |

## Hooks — `.claude/hooks/*.sh`

Deterministic rules. Anything that must *always* happen is a hook; anything requiring judgment is a
skill or subagent.

| Event | Hook | Behavior |
|---|---|---|
| `SessionStart` | `session-start.sh` | Injects the test-env fact, warns on missing `.venv`/`.env`, reports Compose and docs-checkout state |
| `SessionStart(compact)` | `compact-reminder.sh` | Re-injects the invariants most likely lost in a summary |
| `PreToolUse(Edit\|Write)` | `protect-files.sh` | **Blocks** edits to `.env`, `secrets/`, `uv.lock`, generated dirs, and published migrations |
| `PreToolUse(Bash)` | `pytest-env-guard.sh` | **Blocks** a bare `pytest` that would hit the `.env` Postgres, and points at `./scripts/test.sh` |
| `PreToolUse(Bash)` | `git-guard.sh` | **Blocks** force-push, `--no-verify`, `reset --hard`; warns when committing on `main` |
| `PostToolUse(Edit\|Write)` | `ruff-format.sh` | Formats + autofixes the edited `.py`, reports what ruff could not fix |
| `PostToolUse(Edit\|Write)` | `manifest-validate.sh` | Runs `felix validate-manifest` on a changed manifest |
| `PostToolUse(Edit\|Write)` | `settings-sync-reminder.sh` | Names the in-repo companion file a change requires |
| `PostToolUse(Edit\|Write)` | `doc-sync-reminder.sh` | Names the public MDX page a changed surface must update |
| `PostToolUseFailure(Bash)` | `test-failure-hint.sh` | Translates this repo's recurring failures into the actual fix |
| `Stop` | `doc-drift-stop.sh` | Blocks the turn once per drift-set when documented surfaces changed with no doc update |
| `SubagentStop` | `subagent-log.sh` | Appends a delegation audit line to `.claude/logs/` |
| statusLine | `statusline.sh` | branch · dirty count · model · local API health |

CI validates this directory on every change (the `toolkit` job runs
`scripts/validate-toolkit.py`): hook scripts parse and are executable, `settings.json` references
only scripts that exist, subagent frontmatter is well-formed, and skill frontmatter stays inside the
six Agent Skills spec fields.

Test a hook by feeding it its event JSON:

```bash
echo '{"tool_input":{"command":"uv run pytest -q"}}' | .claude/hooks/pytest-env-guard.sh; echo "exit=$?"
bash -n .claude/hooks/*.sh
python3 -c "import json;json.load(open('.claude/settings.json'))"
```

Exit 2 blocks and feeds stderr back to Claude; exit 0 plus
`{"hookSpecificOutput":{"hookEventName":"…","additionalContext":"…"}}` injects context.

## Permissions

`settings.json` pre-approves the read-only and routine loop (`uv run ruff/ty/pytest/felix`, `make`
lint/type/test, the test wrapper, read-only `docker compose` and `gh`), asks before anything that
mutates infrastructure (`make up/down`, migrations, `docker build`, `helm`, `kubectl`, cloud CLIs,
`git push`, `gh pr create/merge`), and denies reading `.env`, `secrets/`, `data/`, `workspace/`,
and `.venv/`.

## Configuration

- `FELIX_DOCS_ROOT` — path to the felix-web docs app when it isn't at
  `~/Projects/felix-web/apps/docs`. Used by `session-start.sh`, `doc-sync-reminder.sh`, and the
  `docs-sync` skill.

## Extending

Ask `felix-dx-maintainer`. Keep hooks fast and silent on the happy path, guard them against a
missing `.venv`/`jq`/non-repo cwd, and keep skill frontmatter inside the six spec fields.
