"""`deploy/gcp/roll.sh` without a terminal: refused up front, or rolled with `--yes`.

The confirmations read `/dev/tty`. Run from CI, an agent's shell or Claude Code's `!` prefix,
there is none to open, and the script used to find out at its first confirmation — after the
production backup had been written — dying on `/dev/tty: Device not configured` (the 0.5.1 roll).

Every test runs the real script with `gh`, `docker` and `gcloud` replaced by stubs on `PATH`
that record their arguments, in a new session so no controlling terminal exists whatever the
test runner has. Nothing here can reach a VM: `gcloud` is the only way the script does, and it
is a stub.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROLL = Path(__file__).resolve().parents[2] / "deploy" / "gcp" / "roll.sh"

STATE = "v0.5.0\n0\nFELIX_IMAGE_TAG=0.5.0\n"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(0o755)


def _run(
    tmp_path: Path, *args: str, fibers: str = "", env_extra: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.write_text("")
    (tmp_path / "fibers").write_text(fibers)
    (tmp_path / "state").write_text(STATE)
    _stub(bin_dir, "gh", "exit 0")
    _stub(bin_dir, "docker", "exit 0")
    # The remote side: canned answers for the two reads whose output steers the script.
    _stub(
        bin_dir,
        "gcloud",
        'case "$*" in\n'
        '  *"describe --tags"*) cat "$STUB_DIR/state" ;;\n'
        '  *"from fibers"*) cat "$STUB_DIR/fibers" ;;\n'
        "esac\nexit 0",
    )
    # The developer's own FELIX_GCP_PROJECT would decide the project these tests assert on.
    inherited = {k: v for k, v in os.environ.items() if k != "FELIX_GCP_PROJECT"}
    env = {
        **inherited,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_DIR": str(tmp_path),
        # Port 9 (discard) on loopback: the health read fails fast and says "unreachable".
        "FELIX_HEALTH_URL": "http://127.0.0.1:9/health",
        # gcloud's own default project, pointing somewhere else: the script must not inherit it.
        "CLOUDSDK_CORE_PROJECT": "some-other-project",
        **(env_extra or {}),
    }
    proc = subprocess.run(
        ["bash", str(ROLL), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )
    return proc, log.read_text()


def test_without_a_terminal_it_refuses_before_touching_anything(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1")
    assert proc.returncode == 2
    assert "no terminal to confirm on" in proc.stderr
    assert "--yes" in proc.stderr
    assert calls == ""


def test_check_needs_no_terminal(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1", "--check")
    assert proc.returncode == 0, proc.stderr
    assert "preflight done, nothing changed" in proc.stdout
    assert "pg_dump" not in calls


def test_yes_gets_past_the_terminal_check(tmp_path: Path) -> None:
    """Order-independent with --check, which proves the flag parsed and the guard let it by."""
    proc, calls = _run(tmp_path, "0.5.1", "--yes", "--check")
    assert proc.returncode == 0, proc.stderr
    assert calls.startswith("gh release view v0.5.1")


def test_yes_does_not_roll_over_durable_runs(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1", "--yes", fibers="abc\tdurable\trunning\t2026-09-30 17:00:00\n")
    assert proc.returncode == 1
    assert "--yes does not roll over them" in proc.stderr
    # Stopped before the backup, so nothing was written on the host.
    assert "pg_dump" not in calls


@pytest.mark.parametrize("args", [("0.5.1", "--force"), ("0.5.1", "yes")])
def test_an_unknown_option_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    proc, calls = _run(tmp_path, *args)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert calls == ""


def _gcloud_calls(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if line.startswith("gcloud ")]


def test_every_gcloud_call_names_the_project(tmp_path: Path) -> None:
    """The VM's name is not unique across projects, so gcloud's default project must not pick it.

    After production moved projects, the old one still held a stopped `felix-api` carrying the
    same tunnel credentials; the 0.6.1 roll had to set `CLOUDSDK_CORE_PROJECT` by hand.
    """
    proc, calls = _run(tmp_path, "0.5.1", "--check")
    assert proc.returncode == 0, proc.stderr
    gcloud = _gcloud_calls(calls)
    assert gcloud, "the preflight reached the VM through gcloud"
    assert all("--project felix-507018" in line for line in gcloud), gcloud
    assert "(felix-507018, us-central1-a)" in proc.stdout


def test_the_project_can_be_overridden(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1", "--check", env_extra={"FELIX_GCP_PROJECT": "my-project"})
    assert proc.returncode == 0, proc.stderr
    gcloud = _gcloud_calls(calls)
    assert gcloud and all("--project my-project" in line for line in gcloud), gcloud
