# Releasing Felix

A release is a tag. `release.yml` runs on `v*.*.*`: it refuses a tag whose version is not the
one the tree carries everywhere, builds the two images for both architectures, pushes them to
GHCR, scans them, attaches an SBOM, signs them with cosign via OIDC, and publishes the GitHub
release from the changelog section. The tag records what shipped *and* performs the shipping —
so it is placed on a merge commit whose CI is already green, never earlier.

Felix follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and `CHANGELOG.md` follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every workspace member shares one version
number; they are versioned together and released together.

## One version, one script

The version is carried by every workspace member's `pyproject.toml` and `__init__.py`, and by
the Helm chart's `version`, `appVersion` and `image.tag`. `v0.2.1` shipped with `image.tag`
pointing at the previous image because those were edited from memory. `scripts/bump-version.py`
is now the list of every location — `tests/unit/test_version_single_source.py` proves the list
matches the files in the tree, and `release.yml` refuses a tag that disagrees with it — so the
list is not repeated here.

```bash
python scripts/bump-version.py --check        # every location agrees; prints the version
python scripts/bump-version.py 0.3.0          # sets every location, then `uv lock`
```

## Procedure

1. **Confirm `main` is green.** Release from `main`, never from a feature branch.

   ```bash
   git switch main && git pull --ff-only
   gh run list --branch main --limit 5
   ```

   This is the starting point, not the thing the tag records — the release commit does not exist
   yet. Step 7 checks that one separately.

2. **Run every gate locally.** CI runs the lean install; the type check needs the extras, so run
   both installs.

   ```bash
   make install-full
   make check                       # ruff check + ty check + pytest + ruff format --check
   uv run felix bundle-manifests
   uv run felix eval --dataset smoke --manifest quick \
     --fixture fixtures/eval/smoke.json --mock
   uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
   make install-full                # restore the full venv afterwards
   ```

3. **Pick the number.** Pre-1.0, a breaking change to a manifest field, an HTTP surface, or a
   `FELIX_` setting is a minor bump; everything else is a patch. Removing or renaming a manifest
   `spec.*` field breaks operators' YAML — treat it as breaking even when the code still parses.

4. **Bump the version**: `python scripts/bump-version.py X.Y.Z` (every location, then `uv lock`).

5. **Close out the changelog.** Rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`, open a fresh
   empty `## [Unreleased]` above it, and add the comparison link at the bottom of the file next to
   the existing `[0.1.0]:` line:

   ```
   [X.Y.Z]: https://github.com/felix-run/felix/releases/tag/vX.Y.Z
   ```

   Keep the Keep a Changelog section order: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
   `Security`. Entries describe what changed for the operator, not which files moved.

6. **Sync the docs before tagging, not after.** Anything user-visible in this release needs its page
   on [docs.felix.run](https://docs.felix.run) updated in the `felix-run/web` repo, plus
   `.env.example` and the README settings coverage for any new `FELIX_` variable.
   `tests/unit/test_invariants.py` fails if a setting is missing from `.env.example`; nothing fails
   if the public docs are stale, which is exactly why this step is easy to skip.

7. **Land the bump through a PR, then tag the merge commit.**

   A release is not an exception to the branch + PR rule
   ([`branch-pr-workflow`](../.claude/skills/branch-pr-workflow/SKILL.md)) — `main` takes no direct
   commits, and that is enforced, so `git commit -am` on `main` simply fails here.

   ```bash
   git switch -c release/vX.Y.Z
   git commit -am "Release vX.Y.Z"
   git push -u origin release/vX.Y.Z
   gh pr create --base main --title "Release vX.Y.Z"
   ```

   Once it merges, tag the merge commit — **after** its CI has gone green, not while it is still
   running:

   ```bash
   git switch main && git pull --ff-only
   gh run list --branch main --limit 2          # find the run for the release commit
   gh run watch <run-id> --exit-status          # blocks; non-zero if it fails
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

   Two things that look like fussiness and are not. **Waiting for CI** is the only cheap moment to
   catch a bad release: a published tag must never be moved or deleted (see *If a release is wrong*),
   so a tag placed on a commit that then goes red cannot be taken back, only superseded. And **push
   the tag by name** rather than `--follow-tags`, which would also try to push `main` — already
   pushed by the merge, and blocked besides.

8. **Watch the release workflow.** Pushing the tag is the release: `release.yml` verifies the
   version, builds and pushes `ghcr.io/felix-run/felix:X.Y.Z` and `:X.Y.Z-gcp` for
   `linux/amd64` and `linux/arm64`, fails on a CRITICAL/HIGH finding in the pushed image,
   attaches an SPDX SBOM, signs both images by digest with cosign (keyless, the workflow's OIDC
   identity), and publishes the GitHub release with the changelog section as its body.

   ```bash
   gh run watch --exit-status $(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
   docker manifest inspect ghcr.io/felix-run/felix:X.Y.Z          # both platforms listed
   cosign verify ghcr.io/felix-run/felix:X.Y.Z \
     --certificate-identity-regexp '^https://github.com/felix-run/felix/' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```

   An empty changelog section fails the release rather than publishing a blank body.

9. **Update `docs/ROADMAP.md`** — mark the shipped items `[x]`, refresh the *Last reviewed* line,
   and fold the completed work into [`docs/HISTORY.md`](HISTORY.md) as a wave entry. The roadmap
   no longer carries a **Shipped** section; a wave entry that lists only wins is not worth
   writing, so record what the wave taught alongside what it delivered.

## After the tag

- **Deployments pin the tag themselves**, via `FELIX_IMAGE_TAG` in the host `.env` and
  `image.tag` in the chart. The chart's value is one of the locations the bump script sets.
- Publishing to PyPI is not wired up. `uv publish` exists but no workflow calls it.
- Watch the scheduled `smoke.yml` run against `api.felix.run` after deploying — it exercises health,
  a sync `/chat`, a durable `202`, and the thinking/lease/search/abort surfaces, and it does not
  block PR CI, so a failure there is easy to miss.
- **Deploying the tag is [`UPGRADING.md`](UPGRADING.md).** A release is not an upgrade: migrations
  and the settings they require move with the image, not after it.

## Dependencies are held back two days

`ci.yml` runs `scripts/check-dependency-age.py --hours 48`: it reads `uv.lock` as written and
asks PyPI when each pinned version was uploaded, failing on anything younger than 48 hours —
the window in which a hijacked package is caught and yanked. A Dependabot bump that lands
inside it fails and passes two days later without a change; a deliberate urgent upgrade
passes `--allow <package>`. (Not `uv lock --exclude-newer`: a timestamp cutoff is a
resolution input, so uv re-resolves from scratch and the lock check fails for reasons that
have nothing to do with age.)

## What the workflow cannot enforce: repo settings

`release.yml` runs the workflow file *at the tag*, so a rewritten workflow arriving on a tag
is not something the workflow can defend against, and a tag can name any commit. The
controls for that live in repository settings, and each is a decision recorded here rather
than in code:

| Setting | What it closes |
|---|---|
| A **tag ruleset** on `refs/tags/v*` restricting creation, update and deletion to a bypass list | Anyone with `push` publishing a signed release; a force-pushed tag moving a released version to new bits |
| A GitHub **environment** on the `image` and `release` jobs with required reviewers | The same, surviving a rewritten workflow file on the tag |
| **Immutable tags** on the GHCR package | A re-published version even if the tag ruleset is bypassed |
| **Require review from code owners** on `main` (plus dismiss stale reviews and require last-push approval) | `.github/CODEOWNERS` is advisory until this is on; with one owner who authors most PRs it also blocks self-merge on those paths without an admin bypass |

`verify` does what a workflow can: the tag's version must be the tree's everywhere, the
commit must be an ancestor of `main`, and the version must not already have a release.

## If a release is wrong

Do not move or delete a published tag — someone may already have pulled it. Cut the next patch
version forward with the fix, and note the retraction in the changelog entry for the new version.
