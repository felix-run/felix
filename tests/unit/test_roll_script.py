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
    tmp_path: Path,
    *args: str,
    fibers: str = "",
    env_extra: dict[str, str] | None = None,
    worker: tuple[int, str] | None = None,
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
        '  *"pg_restore --list"*) echo 7 ;;\n'
        '  *"config_files"*) echo deploy/docker/compose.yml ;;\n'
        '  *"date +%s"*) echo 1000 ;;\n'
        "esac\nexit 0",
    )
    if worker is not None:
        # A whole roll: the API reports the new version and is ready; `worker` is what
        # `/health/worker` answers. `sleep` is a stub so a wait that never ends returns at once.
        (tmp_path / "version").write_text(args[0])
        (tmp_path / "worker_code").write_text(str(worker[0]))
        (tmp_path / "worker_body").write_text(worker[1])
        _stub(
            bin_dir,
            "curl",
            'case "${@: -1}" in\n'
            '  */health/worker) printf \'%s\\n%s\' "$(cat "$STUB_DIR/worker_body")" "$(cat "$STUB_DIR/worker_code")" ;;\n'
            "  */ready) printf 200 ;;\n"
            '  */health) printf \'{"status":"ok","version":"%s"}\' "$(cat "$STUB_DIR/version")" ;;\n'
            "esac",
        )
        _stub(bin_dir, "sleep", "exit 0")
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
    """Deferred, not failed: a CI caller reads 75 as "try again later", not as a broken roll."""
    proc, calls = _run(tmp_path, "0.5.1", "--yes", fibers="abc\tdurable\trunning\t2026-09-30 17:00:00\n")
    assert proc.returncode == 75
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


def test_ssh_flags_reach_every_gcloud_call(tmp_path: Path) -> None:
    """CI has no route to the VM's port 22 but IAP's, so every remote call must carry the flag."""
    proc, calls = _run(
        tmp_path, "0.5.1", "--check", env_extra={"FELIX_SSH_FLAGS": "--tunnel-through-iap --ssh-flag=-v"}
    )
    assert proc.returncode == 0, proc.stderr
    gcloud = _gcloud_calls(calls)
    assert gcloud and all(
        "--quiet --tunnel-through-iap --ssh-flag=-v --command" in line for line in gcloud
    ), gcloud


def test_no_ssh_flags_adds_nothing(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1", "--check", env_extra={"FELIX_SSH_FLAGS": ""})
    assert proc.returncode == 0, proc.stderr
    gcloud = _gcloud_calls(calls)
    assert gcloud and all("--quiet --command" in line for line in gcloud), gcloud


# --- the worker's heartbeat ------------------------------------------------------------------


# The stubbed VM clock reads 1000 s just before `up -d`; a worker started then or later is new.
ROLLED_MS = 1_000_000


def _beat(version: str, started_at: int = ROLLED_MS + 4_000) -> str:
    return (
        f'{{"status":"ok","last_beat_age_s":3,"stale_after_s":60,"version":"{version}",'
        f'"started_at":{started_at}}}'
    )


def test_a_roll_finishes_once_the_new_worker_beats(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "0.5.1", "--yes", worker=(200, _beat("0.5.1")))
    assert proc.returncode == 0, proc.stderr
    assert "worker 0.5.1 beating" in proc.stdout
    assert "Done: v0.5.1" in proc.stdout
    assert "up -d" in calls


def test_a_roll_stops_when_only_the_old_worker_ever_beat(tmp_path: Path) -> None:
    """0.19.0: the old worker's last beat still reads ok for a minute; the version tells them apart."""
    proc, _ = _run(tmp_path, "0.5.1", "--yes", worker=(200, _beat("0.5.0")))
    assert proc.returncode == 1
    assert "no 0.5.1 worker started by this roll beat within 2 minutes" in proc.stderr
    assert "Done:" not in proc.stdout


def test_re_rolling_a_version_stops_on_the_old_workers_beat(tmp_path: Path) -> None:
    """Same version, so the version cannot tell: the beat is from a worker started before the roll."""
    proc, _ = _run(tmp_path, "0.5.1", "--yes", worker=(200, _beat("0.5.1", started_at=ROLLED_MS - 1)))
    assert proc.returncode == 1
    assert "no 0.5.1 worker started by this roll beat" in proc.stderr


def test_a_roll_stops_when_no_worker_beats(tmp_path: Path) -> None:
    never = '{"status":"never","last_beat_age_s":null,"stale_after_s":60,"version":null,"started_at":null}'
    proc, _ = _run(tmp_path, "0.5.1", "--yes", worker=(503, never))
    assert proc.returncode == 1
    assert "(last: 503 " in proc.stderr


@pytest.mark.parametrize("code", [401, 404])
def test_a_version_without_the_route_is_noted_not_stopped(tmp_path: Path, code: int) -> None:
    """Rolling back to a release from before `/health/worker`: under jwt the unknown path is 401."""
    proc, _ = _run(tmp_path, "0.5.1", "--yes", worker=(code, '{"detail":"x"}'))
    assert proc.returncode == 0, proc.stderr
    assert "0.5.1 has no /health/worker" in proc.stdout


@pytest.mark.parametrize("code", [401, 404])
def test_a_version_with_the_route_may_not_go_missing(tmp_path: Path, code: int) -> None:
    """A release that ships the route and answers 401 or 404 lost it, which is no reason to pass."""
    proc, _ = _run(tmp_path, "0.19.2", "--yes", worker=(code, '{"detail":"x"}'))
    assert proc.returncode == 1
    assert f"(last: {code} " in proc.stderr
