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
   git switch -c <type>/<short-slug>     # feat/ fix/ docs/ chore/ refactor/ release/
   ```

2. **Feature-scoped PRs.** The unit of a PR is a feature or audit area, not a single edit. Group
   related changes into one branch; don't open a PR per file. Don't batch unrelated features.

3. **Never stack PRs.** Every PR branches from `main` and targets `main`. If work seems to depend on
   an unmerged PR: put it in the same PR if the pieces aren't independently reviewable, or wait for
   the parent to merge and branch the follow-up from fresh `main`. Say so and stop rather than
   stacking — a stacked PR shows a misleading diff and forces a merge order on the reviewer.

4. **Verify before committing.** `make check` in the edit loop; `make check-ci` before a PR. The
   `felix-dev-loop` skill lists what each covers and which CI jobs neither can reproduce.

   `pre-commit install` (after `make install`) runs on commit: ruff lint and format, trailing
   whitespace, end-of-file, `check-yaml`, `check-added-large-files` (1000 KB) and merge-conflict
   markers. CI runs the same hooks with `--all-files`. Never pass `--no-verify` or `-n` — a
   `PreToolUse` hook blocks both.

   `security.yml` runs gitleaks over the **whole history**, so a realistic fake key committed in a
   test fixture fails every later PR until it is rewritten out. Use tiny obvious placeholders
   (`sk-test`, `x`), and run `gitleaks detect` before pushing a branch that adds credentials-shaped
   strings.

5. **Commit messages**: imperative subject describing the change ("Wire Presidio PII, opt-in LLM
   judges, and Postgres RLS."), body explains *why*, and the Claude co-author trailer:

   ```
   Co-Authored-By: Claude <model name> <noreply@anthropic.com>
   ```

   Use the model name the session's attribution instruction gives; do not copy one from an
   older commit.

6. **Run the quality reviewers.** When the PR changes Python under `apps/`, `packages/`, or
   `tests/`, delegate to **felix-quality-reviewer** on `git diff origin/main...HEAD`, and to
   **felix-test-quality-reviewer** as well when `tests/` changed. Act on the compounding findings or
   say why each one stands, then record the review so the gate passes:

   ```bash
   mkdir -p .claude/logs/quality-review && touch .claude/logs/quality-review/$(git rev-parse HEAD)
   ```

   `pr-quality-gate.sh` is **advisory**: it notes which reviewers have not run on this exact
   commit and exits 0. It was a hard block once and was deliberately softened, because
   re-arming on every amended commit interrupted the flow and "the reviewers it demands are
   worth running on judgement rather than because the turn will not proceed otherwise."

   So the note reappearing after you act on a finding is not an instruction to review again.
   Read it, decide, and move on. One round of review per branch is the norm; act on the
   compounding findings **or say why each one stands**, which is the half of this step that is
   easy to skip. Re-review when the diff has changed shape, not when it has changed.
   "Reviewed, nothing compounding" is a normal result — the reviewers are not graded on
   finding something, and neither is the session.

7. **Open the PR**

   ```bash
   git push -u origin <branch>
   gh pr create --base main --title "<subject>" --body "<why + how tested>"
   ```

   The body follows `.github/PULL_REQUEST_TEMPLATE.md`: why (not only what), how you tested
   (`make check-ci`, Compose smoke, the exact commands), and any `.env.example` / README updates.

   A PR authored by Felix itself (the self-build program, `docs/SELF.md`) is also judged by
   `felix-boundary.yml` running `scripts/felix_boundary.py`: its body must name the ticket it
   implements, and it may not touch the protected paths that script lists.

8. **One live PR at a time.** Several independent PRs that mention each other read as a stack to a
   reviewer. Open the first; keep the rest as drafts and mark each ready once the one before it
   merges.

9. **Merging is the human gate.** Do not merge unless the user explicitly says to.

## Companion updates that belong in the same PR

- New `FELIX_` setting → `.env.example`, README, `compose*.yml`, Helm values
- New governance control → `deploy/GOVERNANCE.md`, `manifests/governed.yaml`
- Model change → an Alembic revision under `migrations/versions/`
- User-visible behavior → an entry under `## [Unreleased]` in `CHANGELOG.md` (Keep a Changelog
  sections: Added, Changed, Deprecated, Removed, Fixed, Security) and a `docs/ROADMAP.md` status
  flip. The file has a union merge in `.gitattributes`, so two pull requests inserting there at
  once merge without a conflict; if git ever does ask, keep both entries — a hand-resolved
  conflict there once dropped six.
- Documented surface → the public MDX pages (docs-sync skill)
- A new or changed store → a `tests/conformance/` arm (postgres-migrations skill)
