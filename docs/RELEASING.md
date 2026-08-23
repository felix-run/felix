# Releasing Felix

Releases are cut by hand. No workflow triggers on tags — `ci.yml` runs on pushes to `main` and on
pull requests only — so nothing is published or built as a side effect of tagging. That is
deliberate: the tag records what shipped, it does not perform the shipping.

Felix follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and `CHANGELOG.md` follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every workspace member shares one version
number; they are versioned together and released together.

## Version lives in nine places

All five `pyproject.toml` files and all four `__init__.py` files must agree, or `felix version`
reports one number while the built wheel carries another.

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

Check them in one pass:

```bash
grep -rn '^version = ' pyproject.toml packages/*/pyproject.toml apps/*/pyproject.toml
grep -rn '__version__ = ' packages/*/src/*/__init__.py apps/*/src/*/__init__.py
```

## Procedure

1. **Confirm `main` is green.** Release from `main`, never from a feature branch.

   ```bash
   git checkout main && git pull
   gh run list --branch main --limit 5
   ```

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

7. **Commit, tag, and push.**

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin main --follow-tags
   ```

8. **Publish the GitHub release**, using the changelog section as the body.

   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
   ```

9. **Update `docs/ROADMAP.md`** — fold the shipped items into **Shipped** and refresh the
   *Last reviewed* line.

## After the tag

- The Docker image is built from `deploy/docker/Dockerfile`; nothing builds it automatically on a
  tag. Build and push it deliberately if the release is meant to produce an image.
- Publishing to PyPI is not wired up either. `uv publish` exists but no workflow calls it.
- Watch the scheduled `smoke.yml` run against `api.felix.run` after deploying — it exercises health,
  a sync `/chat`, a durable `202`, and the thinking/lease/search/abort surfaces, and it does not
  block PR CI, so a failure there is easy to miss.

## If a release is wrong

Do not move or delete a published tag — someone may already have pulled it. Cut the next patch
version forward with the fix, and note the retraction in the changelog entry for the new version.
