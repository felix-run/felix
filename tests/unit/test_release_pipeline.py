"""The release workflow is what `docs/RELEASING.md` says it is."""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _load(name: str) -> dict:
    return YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _by_id(job: dict) -> dict[str, dict]:
    """Steps keyed by `id`, or by `name` for the few without one."""
    return {s.get("id") or s.get("name") or s.get("uses", "").split("@")[0]: s for s in job["steps"]}


def test_release_runs_on_version_tags_only() -> None:
    wf = _load("release.yml")
    on = wf.get("on", wf.get(True))  # PyYAML reads `on` as True; ruamel keeps the string
    assert on["push"]["tags"] == ["v*.*.*"]
    assert "pull_request" not in on and "branches" not in on["push"]


def test_release_permissions_are_minimal_and_per_job() -> None:
    wf = _load("release.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["image"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert wf["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert "permissions" not in wf["jobs"]["verify"], "verify needs nothing beyond the workflow default"


def test_verify_refuses_a_tag_off_main_or_already_released() -> None:
    runs = "\n".join(s.get("run", "") for s in _load("release.yml")["jobs"]["verify"]["steps"])
    assert "scripts/bump-version.py --check" in runs
    assert "git merge-base --is-ancestor" in runs
    assert "releases/tags/" in runs and "could not tell" in runs, "an API failure must not read as unreleased"


def test_no_expression_is_interpolated_into_a_shell_line() -> None:
    """A git ref permits `$()`; values reach `run:` through `env:`, never `${{ }}`."""
    wf = _load("release.yml")
    for job_name, job in wf["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (job_name, step.get("id") or step.get("name"))


def test_images_are_scanned_per_platform_before_the_release_tag_exists() -> None:
    wf = _load("release.yml")
    image = wf["jobs"]["image"]
    variants = {m["variant"]: m for m in image["strategy"]["matrix"]["include"]}
    assert set(variants) == {"plain", "gcp"}
    assert variants["gcp"]["extras"] == "gcp" and variants["plain"]["extras"] == ""
    steps = _by_id(image)
    build = steps["build"]["with"]
    assert build["platforms"] == "linux/amd64,linux/arm64"
    assert "push-by-digest=true" in build["outputs"] and "tags" not in build
    assert build["provenance"] == "mode=max"
    order = list(steps)
    assert order.index("scan") < order.index("tag") < order.index("sign")
    assert 'test "$created" = "$DIGEST"' in steps["tag"]["run"], "the tag must resolve to the scanned index"
    # The build's platform list is the one list: the platforms step asks for exactly it,
    # and every later step loops over the index rather than naming architectures.
    assert steps["platforms"]["env"]["ASKED"] == build["platforms"]
    for step_id in ("scan", "sbom", "sign"):
        run = steps[step_id]["run"]
        # Reading the manifest list, not naming an architecture: an index scan resolves to
        # the runner's own arch and skips the other half of what ships. Matched on the
        # loop's shape rather than one spelling of the IFS assignment.
        assert re.search(r"while IFS=.?=.? read -r platform digest", run), step_id
        assert steps[step_id]["env"]["MANIFESTS"] == "${{ steps.platforms.outputs.manifests }}", step_id
        assert 'done <<< "$MANIFESTS"' in run, step_id
        assert "amd64" not in run and "arm64" not in run, f"{step_id} names an architecture"
    scan = steps["scan"]["run"]
    assert "ghcr.io/aquasecurity/trivy:" in scan and "@sha256:" in scan, (
        "the scanner is a digest-pinned image"
    )
    assert "--exit-code 1" in scan and "--severity CRITICAL,HIGH" in scan
    sbom = steps["sbom"]["run"]
    assert "anchore/syft:" in sbom and "@sha256:" in sbom
    sign = steps["sign"]["run"]
    assert sign.count("cosign sign --yes") == 2, "once for the index, once inside the per-platform loop"
    assert "cosign attest --yes --type spdxjson" in sign


def test_the_release_body_comes_from_the_changelog_section() -> None:
    wf = _load("release.yml")
    notes = next(
        s for s in wf["jobs"]["release"]["steps"] if s.get("name") == "Release notes from the changelog"
    )
    assert 'index($0, "## [" v "]") == 1' in notes["run"], (
        "a literal heading match, not a regex built from the version"
    )
    assert "test -s notes.md" in notes["run"], "an empty changelog section must fail the release"
