"""Background shell commands (`felix.tools.shell_background`), against real supervisors.

What the module promises, one test each: a command outlives the call that started it and is read
incrementally, each byte by one reader; it ends -- with its whole group -- at its timeout, at the
byte budget and when asked; its state is private, outside the workspace and per root; a command
cannot end, hide or free its slot by rewriting that state; and the caps hold under concurrent
starts. No fakes: the thing under test is a detached process tree and the files it leaves.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from felix.tools import shell_background as bg

from tests.support.background_state import isolate_background_state

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    isolate_background_state(monkeypatch, tmp_path_factory)
    yield
    # End anything a test left running; the state directory goes with `tmp_path`.
    for base in bg._state_top().iterdir():
        if base.is_dir() and not base.is_symlink():
            for entry in base.iterdir():
                with contextlib.suppress(OSError):
                    if bg._supervisor_alive(entry):
                        (entry / "kill").touch()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws.resolve()


def _start(root: Path, argv: list[str], *, timeout_s: float = 30, stdin: str | None = None) -> str:
    return bg.start_background(argv, cwd=root, root=root, stdin=stdin, timeout_s=timeout_s, env=ENV)["id"]


def _until(predicate: Callable[[], bool], budget_s: float = 15) -> None:
    deadline = time.monotonic() + budget_s
    while not predicate():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.05)


def _until_done(root: Path, process_id: str, budget_s: float = 15) -> dict[str, Any]:
    """Every read until the command is no longer running, with their output joined."""
    out, err, deadline = "", "", time.monotonic() + budget_s
    while True:
        read = bg.read_background(root, process_id, 1000)
        out, err = out + read["stdout"], err + read["stderr"]
        if read["status"] != "running" or time.monotonic() > deadline:
            return {**read, "stdout": out, "stderr": err}


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _supervisor_pids(entry: Path) -> list[int]:
    found = subprocess.run(["pgrep", "-f", str(entry)], capture_output=True, text=True).stdout
    return [int(p) for p in found.split()]


def _age(entry: Path, name: str, field: str) -> None:
    """Move `entry`'s `name` file's `field` past `KEPT_FOR_S`."""
    data = json.loads((entry / name).read_text())
    data[field] = time.time() - bg.KEPT_FOR_S - 1
    (entry / name).write_text(json.dumps(data))


def test_a_start_answers_at_once_and_the_command_is_read_as_it_writes(root: Path) -> None:
    pid = _start(root, ["sh", "-c", "echo one; sleep 1; echo two >&2; exit 3"])
    first = bg.read_background(root, pid, 5000)
    assert (first["status"], first["stdout"]) == ("running", "one\n")
    done = _until_done(root, pid)
    assert (done["status"], done["exit_code"]) == ("exited", 3)
    assert done["stdout"] == "", "a read returns only what is new"
    assert done["stderr"] == "two\n"


def test_a_read_waits_for_new_output_and_no_longer(root: Path) -> None:
    late = _start(root, ["sh", "-c", "sleep 0.5; echo x; sleep 5"])
    started = time.monotonic()
    assert bg.read_background(root, late, 5000)["stdout"] == "x\n"
    assert time.monotonic() - started < 3, "the read waited past the output"
    silent = _start(root, ["sleep", "5"])
    started = time.monotonic()
    assert bg.read_background(root, silent, 300)["stdout"] == ""
    assert time.monotonic() - started >= 0.3


def test_stdin_and_the_environment_are_what_the_caller_gave(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FELIX_SECRET_FOR_TEST", "leaked")
    pid = _start(root, ["sh", "-c", 'cat; echo "[${FELIX_SECRET_FOR_TEST:-clean}]"'], stdin="piped\n")
    assert _until_done(root, pid)["stdout"] == "piped\n[clean]\n"


def test_kill_ends_the_command_and_every_child_it_started(root: Path, tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    pid = _start(root, ["sh", "-c", f"sleep 30 & echo $! > {child}; wait"])
    _until(lambda: child.exists() and child.read_text().strip() != "")
    grandchild = int(child.read_text())
    ended = bg.kill_background(root, pid)
    assert (ended["status"], ended["exit_code"]) == ("killed", -signal.SIGKILL)
    _until(lambda: _gone(grandchild), budget_s=5)


def test_a_command_that_exits_takes_a_lingering_child_with_it(root: Path, tmp_path: Path) -> None:
    """The group goes when its leader exits, so a backgrounded child cannot hold the pipes and
    keep the supervisor (and the command's status) waiting."""
    child = tmp_path / "child.pid"
    pid = _start(root, ["sh", "-c", f"sleep 30 & echo $! > {child}; echo bye"])
    done = _until_done(root, pid, budget_s=10)
    assert (done["status"], done["stdout"]) == ("exited", "bye\n")
    _until(lambda: _gone(int(child.read_text())), budget_s=5)


def test_a_command_ends_at_its_timeout(root: Path) -> None:
    pid = _start(root, ["sleep", "30"], timeout_s=0.5)
    assert _until_done(root, pid)["status"] == "timed_out"


def test_output_is_held_to_the_foreground_budget(root: Path) -> None:
    pid = _start(root, ["sh", "-c", "yes 0123456789"])
    done = _until_done(root, pid)
    assert done["status"] == "output_exceeded" and done["truncated"]
    entry = bg.background_base(root) / pid
    on_disk = (entry / "out").stat().st_size + (entry / "err").stat().st_size
    assert on_disk == bg.MAX_TOTAL_OUTPUT_BYTES


def test_a_read_returns_at_most_the_tail_of_what_is_new(root: Path) -> None:
    pid = _start(root, ["sh", "-c", "head -c 200000 /dev/zero | tr '\\0' 'x'; echo; echo end"])
    _until(lambda: bg.list_background(root)["processes"][0]["status"] == "exited")
    read = bg.read_background(root, pid, 0)
    assert len(read["stdout"]) == bg.MAX_OUTPUT_BYTES and read["stdout"].endswith("x\nend\n")
    assert read["stdout_skipped"] == 200_005 - bg.MAX_OUTPUT_BYTES


def test_concurrent_readers_are_each_handed_a_byte_once(root: Path) -> None:
    pid = _start(root, ["sh", "-c", "echo hello; sleep 5"])
    _until(lambda: (bg.background_base(root) / pid / "out").stat().st_size > 0)
    got: list[str] = []

    def read() -> None:
        got.append(bg.read_background(root, pid, 0)["stdout"])

    readers = [threading.Thread(target=read) for _ in range(8)]
    for t in readers:
        t.start()
    for t in readers:
        t.join()
    assert sorted(got) == [""] * 7 + ["hello\n"]


def test_a_command_that_cannot_start_says_why(root: Path) -> None:
    pid = _start(root, ["/no/such/binary"])
    done = _until_done(root, pid)
    assert done["status"] == "failed" and done["error"].startswith("FileNotFoundError")


def test_no_more_than_max_running_at_once_even_when_started_together(root: Path) -> None:
    outcomes: list[str] = []

    def start() -> None:
        try:
            outcomes.append(_start(root, ["sleep", "30"]))
        except bg.BackgroundLimitReached:
            outcomes.append("refused")

    starters = [threading.Thread(target=start) for _ in range(3 * bg.MAX_RUNNING)]
    for t in starters:
        t.start()
    for t in starters:
        t.join()
    assert len([o for o in outcomes if o != "refused"]) == bg.MAX_RUNNING
    with pytest.raises(bg.BackgroundLimitReached, match="already running here"):
        _start(root, ["true"])


def test_the_host_cap_counts_every_root(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bg, "MAX_RUNNING_HOST", 2)
    other = (tmp_path / "other").resolve()
    other.mkdir()
    _start(root, ["sleep", "30"])
    _start(other, ["sleep", "30"])
    with pytest.raises(bg.BackgroundLimitReached, match="on this host"):
        _start(root, ["true"])


def test_an_id_is_found_only_under_the_root_that_made_it(root: Path, tmp_path: Path) -> None:
    other = (tmp_path / "other").resolve()
    other.mkdir()
    pid = _start(root, ["true"])
    with pytest.raises(FileNotFoundError):
        bg.read_background(other, pid, 0)
    for bad in ("../" * 11, pid.upper(), pid + "\n", pid[:-1]):
        with pytest.raises(FileNotFoundError):
            bg.read_background(root, bad, 0)


def test_a_linked_entry_is_neither_listed_nor_found(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A fresh entry's shape behind the link, so nothing but the link itself refuses it.
    (elsewhere / "spec.json").write_text(
        json.dumps({"argv": ["true"], "cwd": str(root), "started": time.time()})
    )
    linked = "b" * 32
    os.symlink(elsewhere, bg.background_base(root) / linked)
    assert bg.list_background(root) == {"processes": []}
    with pytest.raises(FileNotFoundError):
        bg.read_background(root, linked, 0)


def test_state_lives_outside_the_workspace_and_is_private(root: Path) -> None:
    pid = _start(root, ["true"])
    base = bg.background_base(root)
    assert root not in base.parents and not any(root.iterdir())
    assert (base / pid).is_dir()
    for d in (base, base.parent):
        assert d.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("make_unsafe", ["open to others", "a link"])
def test_a_state_directory_that_is_not_private_is_refused(
    root: Path, tmp_path: Path, make_unsafe: str
) -> None:
    base = bg.background_base(root)
    if make_unsafe == "open to others":
        os.chmod(base, 0o755)
    else:
        base.rmdir()
        os.symlink(tmp_path, base)
    with pytest.raises(PermissionError, match="refusing"):
        bg.background_base(root)


def test_a_command_rewriting_its_status_neither_ends_it_nor_frees_its_slot(root: Path) -> None:
    pid = _start(root, ["sleep", "30"])
    status = bg.background_base(root) / pid / "status.json"
    for forged in ({}, [], {"state": "exited", "ended": 0, "exit_code": 0}):
        status.write_text(json.dumps(forged))
        assert bg.read_background(root, pid, 0)["status"] == "running"
        assert [p["id"] for p in bg.list_background(root)["processes"]] == [pid]
    assert bg.kill_background(root, pid)["status"] == "killed", "the real outcome replaces the forgery"
    # Once it has ended, a status in no outcome's shape is no outcome.
    status.write_text(json.dumps({"state": "pwned", "ended": time.time(), "exit_code": 0}))
    assert bg.read_background(root, pid, 0)["status"] == "lost"


def test_finished_commands_are_forgotten_after_a_while(root: Path) -> None:
    old, recent = _start(root, ["true"]), _start(root, ["true"])
    _until(lambda: all(p["status"] == "exited" for p in bg.list_background(root)["processes"]))
    _age(bg.background_base(root) / old, "status.json", "ended")
    assert [p["id"] for p in bg.list_background(root)["processes"]] == [recent]
    with pytest.raises(FileNotFoundError):
        bg.read_background(root, old, 0)


def test_at_most_max_kept_are_kept_and_a_running_one_never_makes_room(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bg, "MAX_KEPT", 3)
    running = _start(root, ["sleep", "30"])
    finished = [_start(root, ["true"]) for _ in range(3)]
    _until(lambda: not any(bg._supervisor_alive(bg.background_base(root) / f) for f in finished))
    kept = [p["id"] for p in bg.list_background(root)["processes"]]
    assert kept == [running, *finished[1:]], "the oldest finished one went, the running one stayed"


def test_a_command_whose_supervisor_died_reads_lost_and_ages_out(root: Path) -> None:
    # Short: with its supervisor gone, nothing is left to end it.
    pid = _start(root, ["sleep", "3"])
    entry = bg.background_base(root) / pid
    for supervisor in _supervisor_pids(entry):
        os.kill(supervisor, signal.SIGKILL)
    _until(lambda: bg.read_background(root, pid, 0)["status"] == "lost", budget_s=5)
    _age(entry, "spec.json", "started")
    assert bg.list_background(root) == {"processes": []}


def test_a_start_sweeps_roots_nobody_returns_to(root: Path, tmp_path: Path) -> None:
    abandoned = (tmp_path / "abandoned").resolve()
    abandoned.mkdir()
    pid = _start(abandoned, ["true"])
    _until(lambda: bg.list_background(abandoned)["processes"][0]["status"] == "exited")
    abandoned_base = bg.background_base(abandoned)
    _age(abandoned_base / pid, "status.json", "ended")
    _start(root, ["true"])
    assert not abandoned_base.exists()


def test_first_starts_in_fresh_roots_at_once_all_find_their_root(tmp_path: Path) -> None:
    """A start's sweep removes roots left empty; it must never remove one a concurrent start has
    just made and is about to start in."""
    roots = [(tmp_path / name).resolve() for name in ("a", "b")]
    outcomes: dict[Path, list[str]] = {r: [] for r in roots}

    def start(r: Path) -> None:
        try:
            outcomes[r].append(_start(r, ["sleep", "30"]))
        except bg.BackgroundLimitReached:
            outcomes[r].append("refused")

    for r in roots:
        r.mkdir()
    starters = [threading.Thread(target=start, args=(r,)) for r in roots for _ in range(6)]
    for t in starters:
        t.start()
    for t in starters:
        t.join()
    for r in roots:
        assert len([o for o in outcomes[r] if o != "refused"]) == bg.MAX_RUNNING, outcomes[r]


def test_a_tenants_share_counts_every_root_it_owns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bg, "MAX_RUNNING_OWNER", 2)
    roots = [(tmp_path / name).resolve() for name in ("a", "b", "c")]
    for r in roots:
        r.mkdir()

    def start(r: Path, owner: str) -> str:
        return bg.start_background(
            ["sleep", "30"], cwd=r, root=r, stdin=None, timeout_s=30, env=ENV, owner=owner
        )["id"]

    start(roots[0], "acme")
    start(roots[1], "acme")
    with pytest.raises(bg.BackgroundLimitReached, match="for this tenant"):
        start(roots[2], "acme")
    start(roots[2], "globex")
    start(roots[2], "")  # an unowned start counts against no tenant's share


def test_two_probes_of_a_finished_command_do_not_hold_each_other_off(root: Path) -> None:
    import fcntl

    pid = _start(root, ["true"])
    entry = bg.background_base(root) / pid
    _until(lambda: not bg._supervisor_alive(entry))
    fd = os.open(entry / "alive", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)  # another probe, mid-question
        assert not bg._supervisor_alive(entry)
    finally:
        os.close(fd)


def test_a_start_held_off_by_a_stuck_lock_is_refused_not_hung(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    monkeypatch.setattr(bg, "_LOCK_WAIT_S", 0.3)
    fd = os.open(bg._state_top() / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(bg.BackgroundLimitReached, match="being started"):
            _start(root, ["true"])
        assert time.monotonic() - started < 3
    finally:
        os.close(fd)


def test_an_oversized_state_file_is_read_as_none(root: Path) -> None:
    pid = _start(root, ["true"])
    entry = bg.background_base(root) / pid
    _until(lambda: not bg._supervisor_alive(entry))
    padding = "x" * bg._MAX_STATE_BYTES
    (entry / "status.json").write_text(
        json.dumps({"state": "exited", "ended": time.time(), "exit_code": 0, "error": padding})
    )
    assert bg.read_background(root, pid, 0)["status"] == "lost"


def test_an_entry_whose_start_is_not_written_yet_is_not_forgotten(root: Path) -> None:
    """Between a start's `mkdir` and its spec, a concurrent list must not take it for an old one."""
    fresh = bg.background_base(root) / ("c" * 32)
    fresh.mkdir(mode=0o700)
    assert [p["id"] for p in bg.list_background(root)["processes"]] == [fresh.name]


def test_a_read_waiting_its_turn_spends_its_own_budget_not_more(root: Path) -> None:
    """The wait for a turn and the wait for output share `wait_ms`: a read the hosted gateway gives
    30 seconds never takes the lock wait on top of a 20-second output wait."""
    import fcntl

    pid = _start(root, ["sleep", "30"])
    fd = os.open(bg.background_base(root) / pid / "read.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # a reader that never lets go
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            bg.read_background(root, pid, 1500)
        assert time.monotonic() - started < 3
    finally:
        os.close(fd)
