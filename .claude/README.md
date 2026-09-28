# Felix Claude Code toolkit

Project-scoped configuration for [Claude Code](https://code.claude.com/docs/en/overview), tuned to
this repo: the Felix agents harness (Python 3.14, uv workspace) and its companion docs site in the
separate **felix-web** repo.

```
.claude/
├── settings.json     hook registration, permission allow/ask/deny, status line
├── agents/           subagents (delegated, isolated context)
├── skills/           Agent Skills (agentskills.io format, loaded on demand)
├── hooks/            lifecycle hooks (deterministic enforcement)
│   └── lib/          command.sh (segment/path/workdir helpers), surfaces.sh (surface → docs page)
├── rules/            always-loaded invariants
└── logs/             subagent audit trail (gitignored)
```

## Subagents — `.claude/agents/*.md`

Delegate with the Agent tool or by name. Each runs in its own context and reports back. A
subagent cannot load a skill on demand, so each one **preloads** the skill it depends on through
`skills:` frontmatter, and names any other skill by path (`.claude/skills/<name>/SKILL.md`) rather
than restating it. The agent file holds only the judgment the skill does not.

| Agent | Use for |
|---|---|
| `felix-engineer` | Feature/fix implementation across the harness, API, worker, CLI |
| `felix-postgres` | Schema, Alembic migrations, RLS, pgvector, session log, stores |
| `felix-devops` | Docker/Compose, Helm, AWS/GCP, CI, lean-image and memory budgets |
| `felix-code-reviewer` | Correctness + invariant review of a diff, branch, or PR |
| `felix-security-reviewer` | Tenant isolation, auth/scopes, screening, secrets, SSRF, sandboxes (opus) |
| `felix-quality-reviewer` | Carrying cost: altitude, complexity, dead code, duplication, type/API ergonomics |
| `felix-test-quality-reviewer` | Whether tests are worth having — assertion strength, mocks, brittleness, edges |
| `felix-manifest-architect` | `felix/v1` manifests and schema↔builder wiring |
| `felix-test-engineer` | Unit, e2e, conformance and eval tests under the `memory://` path |
| `felix-dx-maintainer` | Makefile, CLI, pre-commit, and this toolkit |
| `felix-docs-syncer` | In-repo docs + the public Starlight MDX in felix-web |

## Skills — `.claude/skills/*/SKILL.md`

Claude loads a skill when its description matches the task; you can invoke one directly with
`/<name>`. Frontmatter is restricted to the [Agent Skills](https://agentskills.io) spec fields
(`name`, `description`, and optionally `license`, `compatibility`, `metadata`, `allowed-tools`), so
these skills are portable to any skills-compatible agent.

| Skill | Covers |
|---|---|
| `felix-dev-loop` | Install, run, the test tiers, and the three gate tiers (`make check`, `make check-ci`, CI-only) |
| `model-layer` | `felix_ai`, providers and routes, the catalog, caching, metering, decision models and their consumers |
| `manifest-authoring` | Writing `felix/v1` manifests; adding a spec field (+ `references/spec-fields.md`) |
| `governance-pipeline` | The compile pipeline and tool wrapper stack; adding a control |
| `api-surface` | Adding/changing REST, `/v1`, A2A, MCP, and management endpoints |
| `postgres-migrations` | Alembic revisions, RLS, pgvector, in-memory twins |
| `plugin-seam` | Optional features, extras, and the lean-default rule |
| `security-review` | Threat model and control map (+ `references/checklist.md`) |
| `docs-sync` | Surface → doc page mapping across both repos (+ `references/page-map.md`) |
| `deploy-runbook` | Compose overlays, Helm, production configuration checklist |
| `python-conventions` | Style, Protocols, lazy imports, async, ruff/ty exemptions |
| `code-quality` | Complexity budgets, altitude, real vs deliberate duplication (+ `references/felix-hotspots.md`) |
| `dead-code-audit` | Proving unreachability before deleting (+ `references/felix-reachability.md`) |
| `test-quality` | Assertion strength, mocks vs in-memory twins, coverage shape (+ `references/felix-test-map.md`) |
| `branch-pr-workflow` | Branch naming, feature-scoped PRs, commit style, PR gates |

## Hooks — `.claude/hooks/*.sh`

Deterministic rules. Anything that must *always* happen is a hook; anything requiring judgment is a
skill or subagent.

| Event | Hook | Behavior |
|---|---|---|
| `SessionStart` | `session-start.sh` | Injects the test-env fact, warns on missing `.venv`/`.env`, reports Compose and docs-checkout state |
| `SessionStart(compact)` | `compact-reminder.sh` | Re-injects the invariants most likely lost in a summary |
| `PreToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `protect-files.sh` | **Blocks** edits to any `.env*` but `.env.example`, `secrets/`, `uv.lock`, generated dirs, and published migrations; fails closed without `jq` |
| `PreToolUse(Bash)` | `pytest-env-guard.sh` | **Blocks** a bare `pytest` that would hit the `.env` Postgres, and points at `./scripts/test.sh` |
| `PreToolUse(Bash)` | `pr-quality-gate.sh` | Before `gh pr create`, names the reviewers that have not run on this commit — plus `felix-security-reviewer` when the diff touches a control path |
| `PreToolUse(Bash)` | `git-guard.sh` | **Blocks** force-push (`-f`, `-fu`, `+ref`), `--no-verify`/`commit -n`, `reset --hard`, `clean -f` with `-d`/`-x`, whole-tree `checkout`/`restore`, `stash clear`; warns when committing on `main` |
| `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `ruff-format.sh` | Formats + autofixes the edited `.py`, reports what ruff could not fix |
| `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `manifest-validate.sh` | Runs `felix validate-manifest --no-resolve-egress` on the changed manifest, in the tree that owns it |
| `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `settings-sync-reminder.sh` | Names the in-repo companion file a change requires |
| `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `doc-sync-reminder.sh` | Names the public MDX page a changed surface must update (map: `lib/surfaces.sh`) |
| `PostToolUse(Edit\|Write\|MultiEdit\|NotebookEdit)` | `quality-ratchet.sh` | Reports a `.py` whose function/module metrics got worse than at `HEAD` |
| `PostToolUseFailure(Bash)` | `test-failure-hint.sh` | Translates this repo's recurring failures into the actual fix |
| `Stop` | `doc-drift-stop.sh` | Blocks the turn once per drift-set when *this session* changed a documented surface with no doc update. "This session" is measured against the snapshot `session-start.sh` takes, so edits the tree already carried never count |
| `SubagentStop` | `subagent-log.sh` | Appends time, session, agent type and agent id to `.claude/logs/subagents.log` |
| statusLine | `statusline.sh` | branch · dirty count · model · local API health |

### A hook must ask the tree the session is in

`CLAUDE_PROJECT_DIR` is the main checkout, and this repo is routinely worked in from a git
worktree under `.claude/worktrees/`. A hook that treats the project root as the working tree
gets a different repository than the one the session is touching, and every one that did was
wrong in a way nobody noticed:

- `protect-files.sh` failed **open** — `.env`, `uv.lock` and applied migrations were
  editable inside a worktree, because `.claude/worktrees/x/.env` does not match `.env`.
- `quality-ratchet.sh` reported every file as a "new file", because `git show HEAD:<rel>`
  looked for a worktree-prefixed path in the main checkout and found nothing.
- `doc-drift-stop.sh` and `git-guard.sh` reported *another session's* state as this one's.
- `manifest-validate.sh`, `doc-sync-reminder.sh` and `settings-sync-reminder.sh` went silent in
  every worktree, and `ruff-format.sh` formatted with the main checkout's venv — each stripped
  `$CLAUDE_PROJECT_DIR` off the path instead of asking the file's repository.

Two rules follow, and `lib/command.sh` has the helper for each:

- **Given a `file_path`, ask the file's own repository.** `hook_repo_root` / `hook_repo_rel`
  resolve against the worktree that owns the path, not against the project root.
- **Given no path, take `cwd` from the payload.** `hook_workdir` does this (and follows a
  leading `cd`); `cwd` is a documented field on every hook event, `Stop` included.

`tests/unit/test_bash_guard_hooks.py`, `tests/unit/test_file_guard_hooks.py` and
`tests/unit/test_toolkit_hooks.py` assert both in both trees. A guard asserted only in the main checkout is a guard that is absent exactly
where the work happens.

The two quality reviewers also run on pull requests, via
`.github/workflows/quality-review.yml` — it delegates to `felix-quality-reviewer` (and
`felix-test-quality-reviewer` when `tests/` changed) and posts surviving findings as inline PR
comments. It is advisory and never fails the build, skips draft and fork PRs, and exits with a
notice when the `ANTHROPIC_API_KEY` secret is absent.

CI validates this directory on every change (the `toolkit` job runs
`scripts/validate-toolkit.py`; `make toolkit` locally): hook scripts and `hooks/lib/` parse and are
executable, `settings.json` references only scripts that exist, subagent frontmatter is
well-formed and every preloaded skill exists, skill frontmatter stays inside the six Agent Skills
spec fields and every linked `references/*.md` exists — and **every repo path, `file.py:symbol`
and `make` target the Markdown here cites still exists**, and every route module is mapped to a
docs page. The toolkit is prose about the tree; that last check is what keeps it from rotting
unnoticed, as it had (a migration list eleven revisions behind, and a tests directory for evals
that was never there).

Test a hook by feeding it its event JSON:

```bash
echo '{"tool_input":{"command":"uv run pytest -q"}}' | .claude/hooks/pytest-env-guard.sh; echo "exit=$?"
make toolkit
```

Exit 2 blocks and feeds stderr back to Claude; exit 0 plus
`{"hookSpecificOutput":{"hookEventName":"…","additionalContext":"…"}}` injects context.

## The defect shape these guard against

Nearly every real defect in this repo is a control that looks present and does nothing, and the
usual cause is that **the branch production takes is the branch nothing covers**. Four artifacts
target it directly, and `.claude/rules/felix-invariants.md` states the rule so it survives a
compaction:

| Failure | Guard |
|---|---|
| A defaulted parameter every test supplies, so production's own call is uncovered — `create_app()` shipped reading `settings.x` instead of `cfg.x` and died at boot with a green suite | `tests/unit/test_entrypoint_wiring.py` calls each entrypoint the way its console script does, and resolves every `module:attr` string production depends on |
| A test that cannot fail — an AST invariant matched `timeout=<Constant>` while every literal it hunted lived inside `httpx.Timeout(...)` | Mutation: break the rule on purpose and watch the test go red. The **test-quality** skill has the procedure and the three outcomes |
| A control that looks present and does nothing — `replay_safe` was dropped by seven wrappers and had never been `True` on any tool in any manifest | Disable each control in turn and re-run the suite; `tests/unit/test_governance_controls_enforce.py` and `test_no_governance_wrapper_rebuilds_a_tool_by_hand` came out of doing that |
| A security fix that opens a second hole — a validated hostname interpolated into `--host-resolver-rules`, whose grammar is a comma-separated list | `pr-quality-gate.sh` asks for `felix-security-reviewer` when the diff touches a control path; the **security-review** checklist has a grammar-crossing section |
| Deleting live code on a stale note — `SkillRef.description` was nearly removed as unread after `a2a/card.py` started reading it | The **dead-code-audit** skill: absence is the claim that rots fastest, so re-derive it against the tree at HEAD, never from an earlier note |

## Permissions

`settings.json` pre-approves the read-only and routine loop (`uv run ruff/ty/felix`, the `make`
gates — `check`, `check-ci`, `test-cov`, `e2e`, `eval`, `schema`, `bundle` — the test wrapper, the
toolkit validator, read-only `docker compose` and `gh`), asks before anything that
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
