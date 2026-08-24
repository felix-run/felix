---
name: branch-pr-workflow
description: Git and pull-request workflow for the Felix harness repo — branch naming, feature-scoped PRs, commit message style, the pre-commit and CI gates a PR must pass, and the rule against stacking PRs. Use before committing, when asked to commit, ship, open a PR, or start a new piece of work.
allowed-tools: Bash(git:*) Bash(gh:*) Read Grep
---

# Branch + PR workflow

Land work on a branch and open a PR into `main`. `main` is the release source; keep it green.
Commit or push **only when the user asks**.

## Procedure

1. **Branch from fresh main**

   ```bash
   git switch main && git pull --ff-only origin main
   git switch -c <type>/<short-slug>     # feat/ fix/ docs/ chore/ refactor/
   ```

2. **Feature-scoped PRs.** The unit of a PR is a feature or audit area, not a single edit. Group
   related changes into one branch; don't open a PR per file. Don't batch unrelated features.

3. **Never stack PRs.** Every PR branches from `main` and targets `main`. If work seems to depend on
   an unmerged PR: put it in the same PR if the pieces aren't independently reviewable, or wait for
   the parent to merge and branch the follow-up from fresh `main`. Say so and stop rather than
   stacking — a stacked PR shows a misleading diff and forces a merge order on the reviewer.

4. **Verify before committing** — the same gates CI runs:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run ty check packages apps
   ./scripts/test.sh
   uv run felix bundle-manifests            # when manifests/ or the schema changed
   ```

   `pre-commit install` (after `make install`) runs the ruff hooks on commit. Never pass
   `--no-verify` — a `PreToolUse` hook blocks it, and CI re-runs the same checks.

5. **Commit messages**: imperative subject describing the change ("Wire Presidio PII, opt-in LLM
   judges, and Postgres RLS."), body explains *why*, and the Claude co-author trailer:

   ```
   Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
   ```

6. **Run the quality reviewers.** When the PR changes Python under `apps/`, `packages/`, or
   `tests/`, delegate to **felix-quality-reviewer** on `git diff origin/main...HEAD`, and to
   **felix-test-quality-reviewer** as well when `tests/` changed. Act on the compounding findings or
   say why each one stands, then record the review so the gate passes:

   ```bash
   mkdir -p .claude/logs/quality-review && touch .claude/logs/quality-review/$(git rev-parse HEAD)
   ```

   `pr-quality-gate.sh` blocks `gh pr create` until that marker exists for the exact commit, so a
   new or amended commit asks for a fresh review. "Reviewed, nothing compounding" is a normal
   result — the reviewers are not graded on finding something.

7. **Open the PR**

   ```bash
   git push -u origin <branch>
   gh pr create --base main --title "<subject>" --body "<why + how tested>"
   ```

   The body follows `.github/PULL_REQUEST_TEMPLATE.md`: why (not only what), how you tested
   (`make check`, Compose smoke, the exact commands), and any `.env.example` / README updates.

7. **Merging is the human gate.** Do not merge unless the user explicitly says to.

## Companion updates that belong in the same PR

- New `FELIX_` setting → `.env.example`, README, `compose*.yml`, Helm values
- New governance control → `deploy/GOVERNANCE.md`, `manifests/governed.yaml`
- Model change → an Alembic revision under `migrations/versions/`
- User-visible behavior → `CHANGELOG.md` (Unreleased) and a `docs/ROADMAP.md` status flip
- Documented surface → the public MDX pages (docs-sync skill)
