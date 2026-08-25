# Releasing Felix

Releases are cut by hand. No workflow triggers on tags — `ci.yml` runs on pushes to `main` and on
pull requests only — so nothing is published or built as a side effect of tagging. That is
deliberate: the tag records what shipped, it does not perform the shipping.

Felix follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and `CHANGELOG.md` follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every workspace member shares one version
number; they are versioned together and released together.

## Version lives in twelve places

All five `pyproject.toml` files and all four `__init__.py` files must agree, or `felix version`
reports one number while the built wheel carries another. The Helm chart carries three more, and
they are the ones that get forgotten: they are not Python, so the greps below never covered them.

| File | Field |
|---|---|
| `pyproject.toml` | `version` |
| `packages/harness/pyproject.toml` | `version` |
| `packages/cli/pyproject.toml` | `version` |
| `apps/api/pyproject.toml` | `version` |
| `apps/worker/pyproject.toml` | `version` |
| `packages/harness/src/felix/__init__.py` | `__version__` |
| `packages/cli/src/felix_cli/__init__.py` | `__version__` |
| `apps/api/src/felix_api/__init__.py` | `__version__` |
| `apps/worker/src/felix_worker/__init__.py` | `__version__` |
| `deploy/helm/felix/Chart.yaml` | `version` |
| `deploy/helm/felix/Chart.yaml` | `appVersion` |
| `deploy/helm/felix/values.yaml` | `image.tag` |

`values.yaml`'s `image.tag` is the one that does damage when it is missed: `helm install` from the
release tree then deploys the *previous* image, so an operator upgrading gets the version the
release was meant to replace. `v0.2.1` shipped that way.

Check them in one pass:

```bash
grep -rn '^version = ' pyproject.toml packages/*/pyproject.toml apps/*/pyproject.toml
grep -rn '__version__ = ' packages/*/src/*/__init__.py apps/*/src/*/__init__.py
grep -rnE '^(version|appVersion):|^  tag:' deploy/helm/felix/Chart.yaml deploy/helm/felix/values.yaml
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

4. **Bump all nine files** to the new version.

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

8. **Publish the GitHub release**, using the changelog section as the body.

   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(
     awk '/^## \[X.Y.Z\]/{f=1; next} /^## \[/{f=0} f' CHANGELOG.md
   )
   ```

   `awk` rather than `sed -n '/.../,/## \[/p'`: a `sed` range is inclusive of its end, so that
   version published the *next* release's heading as the last line of the body.

9. **Update `docs/ROADMAP.md`** — fold the shipped items into **Shipped** and refresh the
   *Last reviewed* line.

## After the tag

- The Docker image is built from `deploy/docker/Dockerfile`; nothing builds it automatically on a
  tag. Build and push it deliberately if the release is meant to produce an image.
- Publishing to PyPI is not wired up either. `uv publish` exists but no workflow calls it.
- Watch the scheduled `smoke.yml` run against `api.felix.run` after deploying — it exercises health,
  a sync `/chat`, a durable `202`, and the thinking/lease/search/abort surfaces, and it does not
  block PR CI, so a failure there is easy to miss.
- **Deploying the tag is [`UPGRADING.md`](UPGRADING.md).** A release is not an upgrade: migrations
  and the settings they require move with the image, not after it.

## If a release is wrong

Do not move or delete a published tag — someone may already have pulled it. Cut the next patch
version forward with the fix, and note the retraction in the changelog entry for the new version.
