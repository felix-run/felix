"""The way production starts, exercised the way production starts it.

`create_app()` takes `settings` as an optional keyword and resolves it into
`cfg = settings or get_settings()`. A line reading `settings.bundled_only` instead of
`cfg.bundled_only` shipped in an image that raised `AttributeError: 'NoneType' object has no
attribute 'bundled_only'` before serving a request. The whole suite stayed green, because
every test passes `settings=` explicitly and production is the only caller that does not.
It was found by booting the image for an unrelated reason.

That is one instance of a class this repo keeps producing: **the branch production takes is
the branch nothing covers.** A defaulted parameter every caller supplies, and a string naming
a callable that no import statement mentions — a console-script target, an ASGI factory path,
a Taskiq broker or module path, a container `command:` — are both invisible to ruff, to `ty`,
and to a suite whose callers all pass the argument. The failure is always the same: the
process dies at startup, in an image, after review.

So the checks here are deliberately not about behavior. They resolve every stringly-typed
reference production depends on, and they call the entrypoints with production's own
argument list — which is to say, with no arguments at all.

Everything is discovered rather than listed. An earlier version of this file named the five
distributions, two entrypoint modules and three worker functions in hand-maintained tuples —
which is the "list naming six of nine governance wrappers" failure this repo already has on
record, rebuilt inside the guard against it. Adding `apps/gateway` would have silently
narrowed every check while `>= 5` stayed green.

`EXPECTED_COMMANDS` is the one deliberate exception, and its comment says why: which files
start Felix is a deployment fact, not something a scan can infer — a Compose file is not
distinguishable from any other YAML by inspection. It is asserted in both directions, so it
can neither outlive the files it names nor miss a new one.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

# A `module:attr` reference — the form shared by console scripts, the Granian/uvicorn factory
# argument, and Taskiq's `--broker`/`--scheduler` paths.
TARGET = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
# A command that claims to be one of ours. Deliberately wider than the console-script
# names: `^felix(-[a-z-]+)?$` could not match the Helm migrate job's bare `felix` at all, and
# once widened to include it, `felixx` still slipped through un-collected — so a typo in the
# one command using the bare name was invisible rather than red. Anything felix-prefixed is
# collected here and then has to *be* a console script, which is the assertion that bites.
FELIX_BINARY = re.compile(r"^felix[\w-]*$")

# Which deployment surface runs which binaries. A hand-written list, and one of the few that
# earns it: "which files start Felix" is a deployment fact, not something derivable from the
# tree — a Compose file is not distinguishable from any other YAML by inspection. Both
# directions are asserted against it below, so it cannot outlive the files it names *or* miss
# a new one.
#
# Keyed by file, because a set of binaries was the hole: Compose, the Dockerfile and the Helm
# Deployment all yield `felix-api`, so any one of them could stop being parsed entirely and
# hide behind the others. Deleting all three Compose commands, replacing the Dockerfile CMD,
# and templating the Helm commands away were each independently green against a comment
# claiming per-service coverage.
#
# In document order, because even per-file a *set* cannot see two commands exchanged between
# services: give `api:` the worker's command and vice versa and the same three names come
# back, from a deployment where nothing listens on 8080 and the healthcheck restart-loops.
# Three near-identical blocks in one file is the copy-paste these formats invite.
EXPECTED_COMMANDS = {
    "deploy/docker/Dockerfile": ["felix-api"],
    "deploy/docker/compose.yml": ["felix-api", "felix-worker", "felix-scheduler"],
    # The Temporal overlay is the only deploy surface that runs the fourth binary. It has
    # no `felix-api` of its own — it layers onto compose.yml, which supplies the rest.
    "deploy/docker/compose.temporal.yml": ["felix-temporal-worker"],
    "deploy/helm/felix/templates/deployment.yaml": ["felix-api", "felix-worker", "felix-scheduler"],
    "deploy/helm/felix/templates/migrate-job.yaml": ["felix"],
}


def _distributions() -> list[Path]:
    """Every workspace member, discovered. The root pyproject declares `packages/*`, `apps/*`."""
    return sorted(
        p
        for base in ("packages", "apps")
        for p in (ROOT / base).iterdir()
        if (p / "pyproject.toml").is_file()
    )


def _console_scripts() -> dict[str, str]:
    """Every `[project.scripts]` entry in the workspace, name -> `module:attr`."""
    found: dict[str, str] = {}
    for dist in _distributions():
        data = tomllib.loads((dist / "pyproject.toml").read_text(encoding="utf-8"))
        found.update(data.get("project", {}).get("scripts", {}))
    return found


def _resolve(target: str) -> Any:
    """Import `module:attr` the way an installed console script does, or fail saying so."""
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"{target}: module {module_name} does not import ({exc})")
    if not hasattr(module, attr):
        pytest.fail(f"{target}: {module_name} has no attribute {attr!r} — this reference is dead")
    return getattr(module, attr)


def _entrypoint_modules() -> list[Path]:
    """The source file behind each console script, derived from the scripts themselves."""
    paths = []
    for target in sorted(set(_console_scripts().values())):
        module = importlib.import_module(target.partition(":")[0])
        file = getattr(module, "__file__", None)
        if file:
            paths.append(Path(file))
    return sorted(set(paths))


def _module_attr_literals(path: Path) -> set[str]:
    """Every `module:attr`-shaped string literal in a source file.

    Deliberately not scoped to a particular call. Which function receives the string is the
    part most likely to change — Granian to uvicorn, `WorkerArgs` to a CLI invocation — while
    the string itself stays exactly as load-bearing. Matching the shape rather than the callee
    keeps this working across that change instead of quietly matching nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and TARGET.match(node.value)
    }


def _module_list_literals(path: Path) -> set[str]:
    """Plain module paths passed as `modules=[...]`, which carry no colon.

    Taskiq is told `modules=["felix_worker.tasks"]` separately from `broker=`. Rename that
    module and the worker starts with zero registered cron tasks — nothing fails, nothing
    fires. `TARGET` requires a colon, so this form was invisible to the scan above.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.keyword) and node.arg == "modules"):
            continue
        for element in getattr(node.value, "elts", []):
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.add(element.value)
    return found


def _first_token(value: str) -> str:
    """The binary a `CMD`/`command:` value would exec, in any form Compose and Helm accept.

    All of these name `felix-api`, and an earlier version of this test — a regex for a
    *quoted* `felix-*` token anywhere in the file — got two of them wrong in opposite
    directions. It missed `command: felix-api`, the bare YAML scalar and the most common way
    this is written, so the rename it exists to catch went undetected. And it fired on a
    quoted volume key like `"felix-data":`, reporting that a Docker volume would exit 127 —
    so a formatter that quotes keys turned the suite red for nothing.
    """
    text = value.strip().lstrip("[").strip()
    if text.startswith("-") and not text.startswith("--"):
        text = text[1:].strip()  # a YAML block-list entry
    token = re.split(r"[,\s]", text, maxsplit=1)[0].strip("[]\"'")
    # The basename. Nothing in `deploy/` is path-qualified today — the Dockerfile puts
    # `/app/.venv/bin` on PATH and uses the bare name — but writing it out is a legal edit,
    # and the guard must not fail one. It did: `re.match(r"(^|/)felix…")` anchors at position
    # 0, so it rejected `/app/.venv/bin/felix-api` and accepted `/felix-api`.
    return token.rsplit("/", 1)[-1]


def _container_commands() -> list[tuple[str, int, str]]:
    """(file, line, binary) for every container command that runs a felix console script."""
    files = sorted(
        p
        for p in DEPLOY.rglob("*")
        if p.is_file() and (p.name.startswith("Dockerfile") or p.suffix in {".yml", ".yaml"})
    )
    directive = re.compile(r"^\s*(?:CMD|ENTRYPOINT)\s+(.*)$|^\s*(?:command|entrypoint):\s*(.*)$")
    found: list[tuple[str, int, str]] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            match = directive.match(line)
            if not match:
                continue
            # Single-line flow sequences only (`CMD ["felix-api"]`, `command: ["felix-worker"]`),
            # which is the one command syntax this tree uses. A multi-line form would yield no
            # token, drop the file out of `by_file`, and fail the equality assertion below —
            # loudly, and with a message naming the file. That is a better deal than a
            # block-scanning branch no assertion reaches.
            value = (match.group(1) or match.group(2) or "").strip()
            token = _first_token(value)
            if FELIX_BINARY.match(token):
                found.append((str(path.relative_to(ROOT)), i + 1, token))
    return found


def test_every_console_script_target_resolves() -> None:
    """The names a container runs, pinned as a set rather than a count.

    Nothing imports these paths, so renaming or moving the function they name is a change
    ruff and `ty` both accept and every process rejects at exec time. The set is exact: a
    floor of `>= 5` stayed green when one script was dropped and another added.

    Arity is checked here rather than in a worker-specific test, because it is a property of
    *being* a console script: the shell invokes it with nothing, so a required parameter is a
    `TypeError` at startup. That applies equally to `felix-api` and `felix-temporal-worker`.
    """
    scripts = _console_scripts()
    assert set(scripts) == {
        "felix",
        "felix-api",
        "felix-scheduler",
        "felix-temporal-worker",
        "felix-worker",
    }, f"the workspace's console scripts changed: {sorted(scripts)}"

    for name, target in sorted(scripts.items()):
        assert TARGET.match(target), f"{name} = {target!r} is not a module:attr reference"
        entry = _resolve(target)
        assert callable(entry), f"{name} -> {target} resolves to something not callable"

        # `felix` resolves to a Typer app: callable, not a function, and its parameters
        # belong to click. Named rather than skipped by shape — `if not isfunction: continue`
        # would silently drop the arity check for a future script pointing at a partial or a
        # callable class, which is the quiet-narrowing this file argues against.
        if not inspect.isfunction(entry):
            assert name == "felix", (
                f"{name} -> {target} is callable but not a function, so its arity is unchecked. "
                "Only the Typer app is expected to be in that position."
            )
            continue
        required = [
            param
            for param, spec in inspect.signature(entry).parameters.items()
            if spec.default is inspect.Parameter.empty
            and spec.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        assert not required, f"{name} -> {target} needs {required}, but a console script passes nothing"


def test_every_module_reference_in_an_entrypoint_module_resolves() -> None:
    """The ASGI factory path, the Taskiq broker and scheduler paths, and the module list.

    `felix_api.main` hands Granian the string `"felix_api.main:create_application"`, and
    `felix_worker.main` hands Taskiq `"felix_worker.tasks:broker"`, `":scheduler"`, and
    `modules=["felix_worker.tasks"]`. Each is a callable or module named in a string in the
    same file that would otherwise have imported it, which is the one form of reference an
    editor's rename leaves behind.
    """
    modules = _entrypoint_modules()
    # Exact, like the console-script set: a floor at the current value cannot see one module
    # disappearing while another arrives.
    assert len(modules) == 3, f"expected a source file per console-script module, found {modules}"

    targets = 0
    for module_path in modules:
        for target in sorted(_module_attr_literals(module_path)):
            _resolve(target)
            targets += 1
        for name in sorted(_module_list_literals(module_path)):
            try:
                importlib.import_module(name)
            except ImportError as exc:  # pragma: no cover - the message is the point
                pytest.fail(f"{module_path.name} names module {name!r}, which does not import ({exc})")
            targets += 1
    assert targets == 4, (
        "expected exactly the factory, broker, scheduler and module paths; resolved "
        f"{targets} — a reference was added or lost"
    )


def test_the_containers_run_a_console_script_that_exists() -> None:
    """Every `CMD` and `command:` in deploy/, Compose and Helm alike.

    A console script renamed in `pyproject.toml` and not in the Dockerfile produces an image
    that builds, pushes, and then exits 127. The Helm chart runs the same three binaries and
    is what `deploy/aws` and `deploy/gcp` both point at, so scanning only `deploy/docker`
    caught the rename for Compose and missed it for Kubernetes.
    """
    scripts = set(_console_scripts())
    commands = _container_commands()
    assert commands, "no container command names a felix console script — this scan found nothing"

    for where, line, binary in commands:
        assert binary in scripts, (
            f"{where}:{line} runs {binary!r}, which is not a console script in this workspace "
            f"(have: {sorted(scripts)}) — the container would exit 127"
        )

    # Per *file*. A set of binaries was the hole: the same three names come from Compose, the
    # Dockerfile and the Helm Deployment, so any one of those could go dark behind the others
    # and the union stayed complete.
    by_file: dict[str, list[str]] = {}
    for where, _, binary in sorted(commands, key=lambda row: (row[0], row[1])):
        by_file.setdefault(where, []).append(binary)

    # Both directions. `<=` on a per-file set was one-directional: a *new* deploy surface
    # simply would not be in the dict, so if a later parser change blinded it, nothing failed
    # — the same hole per-file keying was introduced to close, one file over.
    assert set(by_file) == set(EXPECTED_COMMANDS), (
        "the set of deploy files running a felix binary changed. Added: "
        f"{sorted(set(by_file) - set(EXPECTED_COMMANDS))}; no longer found: "
        f"{sorted(set(EXPECTED_COMMANDS) - set(by_file))}"
    )
    for rel, expected in sorted(EXPECTED_COMMANDS.items()):
        assert (ROOT / rel).is_file(), f"EXPECTED_COMMANDS names {rel}, which no longer exists"
        assert by_file[rel] == expected, (
            f"{rel} runs {by_file[rel]} in document order, expected {expected} — a command was "
            "renamed, exchanged between services, or stopped being parsed"
        )


def test_the_api_boots_with_the_arguments_production_passes() -> None:
    """None. Production passes none.

    `create_application()` is what Granian and uvicorn call, and it forwards nothing to
    `create_app()`. Every other caller in this repo passes `settings=`, so this is the only
    place the `settings or get_settings()` branch is taken.

    `tests/integration/test_http_surfaces.py` covers `create_app()` with no settings — the fix
    for the shipped bug. What is uncovered without this, and the only reason it is here, is
    the line of `felix_api.main` that turns Granian's factory string into that call.

    It does *not* pin that production composes the installed plugins, though it goes through
    the path that would: `installed_plugins()` returns `[]` in the default venv, so
    `create_app(plugins=[])` and the real call produce the same app. An earlier version of
    this docstring claimed the plugin composition as the reason the test exists, and a
    reviewer showed that rewriting the factory to pass `plugins=[]` left it green. Pinning
    that needs a plugin installed — `tests/unit/test_plugin_entry_point.py` is where it lives.
    """
    from felix_api.main import create_application

    # Booting populates two process globals — the `get_settings` lru_cache and the plugin
    # registry. Resetting them is `_reset_app_globals` in tests/conftest.py, with the rest of
    # this suite's isolation, rather than a `finally` only this test knows to write.
    app = create_application()

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/health" in paths, f"the app booted without its health route: {sorted(paths)}"
    # `database_url` has a non-empty default, so asserting it is truthy pins nothing — it is
    # true of any Settings object, resolved from the environment or not. The scheme is what
    # shows the resolution actually happened under scripts/test.sh.
    assert app.state.settings.database_url.startswith("memory://"), (
        f"create_app() did not resolve settings from the environment: {app.state.settings.database_url}"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('["felix-api"]', "felix-api"),
        ('["felix", "migrate", "head"]', "felix"),
        ("felix-api", "felix-api"),
        ("- felix-api", "felix-api"),
        ('- "felix-api"', "felix-api"),
        ('["/app/.venv/bin/felix-api"]', "felix-api"),
        ('["python", "-m", "http.server"]', "python"),
        ("server /data --console-address :9001", "server"),
        ('["valkey-server", "--maxmemory", "64mb"]', "valkey-server"),
    ],
)
def test_a_command_value_yields_the_binary_it_would_exec(value: str, expected: str) -> None:
    """The extraction itself, pinned.

    Nothing checked this, and it shipped wrong in both directions: a regex anchored with
    `re.match(r"(^|/)felix…")` cannot consume anything but a *leading* slash, so the
    path-qualified form the Dockerfile blesses failed the guard while
    `/app/.venv/bin/felix-apii` passed it. A parser with no test is the shape this whole file
    is about.
    """
    assert _first_token(value) == expected


def test_every_process_that_runs_a_turn_hydrates_secrets() -> None:
    """`hydrate_secrets` populates the process-global redaction list, and three of the four
    sinks — fiber state, audit payloads, session events — redact through
    `collected_secret_values()` with no settings, so they see nothing else.

    The API and the Taskiq worker always called it. `felix temporal-worker` did not, and it
    registers the `fiber_step` activity, which runs a full agent turn in that process — so a
    credential echoed into a tool result was persisted verbatim there and served back
    through session export and fiber resume. An entrypoint that runs turns is the unit that
    has to hydrate.
    """
    sources = {
        "apps/api/src/felix_api/app.py": "the API lifespan",
        "apps/worker/src/felix_worker/tasks.py": "the Taskiq worker startup",
        "packages/harness/src/felix/durability/temporal.py": "the Temporal worker",
    }
    missing = [
        f"{path} ({what})"
        for path, what in sources.items()
        if "hydrate_secrets" not in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert missing == [], (
        "a process that runs agent turns does not hydrate secrets, so its audit, session "
        "and fiber redaction see an empty list:\n  " + "\n  ".join(missing)
    )
