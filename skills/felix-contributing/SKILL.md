---
name: felix-contributing
description: How a change becomes a pull request in the Felix repo — branch naming, feature-scoped PRs, the rule against stacking, the gates a change must pass before it ships, and what belongs in the PR body. Use when preparing a change for review, opening a pull request, or deciding how to scope work.
---

> This is a summary. The source of truth is `.claude/skills/branch-pr-workflow/SKILL.md` — read it with `read_file` when
> the two disagree, and trust the checkout over this file.
> The commands below are what a human or CI runs on your change. You cannot run
> them: you have no shell. Name the ones that still need running instead of
> reporting them as done.

# Contributing to Felix

Land work on a branch and open a pull request into `main`. `main` is the release source; keep it
green. Never commit or push unless you were asked to.

## Branch

Branch from fresh `main`, one branch per feature or audit area:

    <type>/<short-slug>     # feat/ fix/ docs/ chore/ refactor/ release/

## Scope

The unit of a pull request is a feature or an audit area, not a single edit. Group related
changes into one branch. Do not open a pull request per file, and do not batch unrelated
features into one.

**Never stack pull requests.** Every branch starts from `main` and targets `main`. If work seems
to depend on an unmerged pull request, either put it in the same pull request — when the pieces
are not independently reviewable — or wait for the parent to merge and branch the follow-up from
fresh `main`. Say so and stop rather than stacking: a stacked diff is misleading and forces a
merge order on the reviewer.

## Gates a change must pass

The same checks CI runs:

    uv run ruff check . && uv run ruff format --check .
    uv run ty check packages apps
    ./scripts/test.sh
    uv run felix bundle-manifests            # when manifests/ or the schema changed

Never bypass the commit hooks. CI re-runs the same checks, so skipping them locally only moves
the failure later.

If the change touches a manifest field or the schema, run `make schema` — the checked-in
`schemas/manifest.schema.json` is generated, and a stale copy fails the build.

## Commit messages

An imperative subject describing the change, and a body that explains *why* rather than
restating the diff.

## Pull request body

Say why the change exists, not only what it does. State how you tested it, naming the exact
commands you ran. Call out any `.env.example`, README, or CHANGELOG updates the change required —
and if a documented surface changed and you did not update its documentation, say that explicitly
instead of leaving it silent.

## Reporting honestly

If you could not verify something, say which part and why. A change described as tested when it
was not is worse than a change described as untested. If a gate failed, quote the failure rather
than summarizing it as a pass.
