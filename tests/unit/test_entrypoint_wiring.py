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
a Taskiq broker path — are both invisible to ruff, to `ty`, and to a suite whose callers all
pass the argument. The failure is always the same: the process dies at startup, in an image,
after review.

So the checks here are deliberately not about behavior. They resolve every stringly-typed
reference production depends on, and they call the entrypoints with production's own
argument list — which is to say, with no arguments at all.
"""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"
COMPOSE_DIR = ROOT / "deploy" / "docker"

# Every workspace member that ships an installable entry point.
DISTRIBUTIONS = (
    ROOT / "packages" / "ai",
    ROOT / "packages" / "harness",
    ROOT / "packages" / "cli",
    ROOT / "apps" / "api",
    ROOT / "apps" / "worker",
)

# A `module:attr` reference, the form shared by console scripts, the Granian/uvicorn
# factory argument, and Taskiq's `--broker`/`--scheduler` paths.
TARGET = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def _console_scripts() -> dict[str, str]:
    """Every `[project.scripts]` entry in the workspace, name -> `module:attr`."""
    found: dict[str, str] = {}
    for dist in DISTRIBUTIONS:
        pyproject = dist / "pyproject.toml"
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
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


def _string_arguments(path: Path) -> set[str]:
    """Every `module:attr`-shaped string literal in a source file.

    Deliberately not scoped to a particular call. Which function receives the string is the
    part most likely to change — Granian to uvicorn, `WorkerArgs` to a CLI invocation — while
    the string itself stays exactly as load-bearing. Matching the shape rather than the
    callee keeps this working across that change instead of quietly matching nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and TARGET.match(node.value)
    }


def test_every_console_script_target_resolves() -> None:
    """`felix-api`, `felix-worker`, `felix-scheduler`, `felix` — the names a container runs.

    Nothing imports these paths, so renaming or moving the function they name is a change
    ruff and `ty` both accept and every process rejects at exec time.
    """
    scripts = _console_scripts()
    assert len(scripts) >= 5, f"expected the workspace's console scripts, found {sorted(scripts)}"

    for name, target in sorted(scripts.items()):
        assert TARGET.match(target), f"{name} = {target!r} is not a module:attr reference"
        assert callable(_resolve(target)), f"{name} -> {target} resolves to something not callable"


def test_every_module_attr_string_in_an_entrypoint_module_resolves() -> None:
    """The ASGI factory path and the Taskiq broker/scheduler paths.

    `felix_api.main` hands Granian the string `"felix_api.main:create_application"`, and
    `felix_worker.main` hands Taskiq `"felix_worker.tasks:broker"` and `":scheduler"`. Each is
    a callable named in a string in the same file that would have imported it, which is the
    one form of reference an editor's rename leaves behind.
    """
    entrypoint_modules = [
        ROOT / "apps" / "api" / "src" / "felix_api" / "main.py",
        ROOT / "apps" / "worker" / "src" / "felix_worker" / "main.py",
    ]
    seen = 0
    for module_path in entrypoint_modules:
        assert module_path.is_file(), f"{module_path} moved; this invariant now checks nothing"
        for target in sorted(_string_arguments(module_path)):
            _resolve(target)
            seen += 1
    assert seen >= 3, f"expected the factory and broker paths, resolved {seen} — the scan found nothing"


def test_the_containers_run_a_console_script_that_exists() -> None:
    """`CMD ["felix-api"]` and every Compose `command:` naming a `felix-*` binary.

    A console script renamed in `pyproject.toml` and not in the Dockerfile produces an image
    that builds, pushes, and then exits 127.
    """
    scripts = set(_console_scripts())
    files = [DOCKERFILE, *sorted(COMPOSE_DIR.glob("compose*.yml"))]
    assert len(files) >= 3, f"expected the Dockerfile and the Compose overlays, found {files}"

    named: list[tuple[str, str]] = []
    for path in files:
        # Both `CMD ["felix-api"]` and `command: ["felix-api"]` quote the binary first, so
        # one pattern covers them. A bare `felix-` token elsewhere in the file (an image
        # tag, a volume, a comment) is not quoted and does not match.
        for match in re.finditer(r'["\'](felix-[a-z-]+)["\']', path.read_text(encoding="utf-8")):
            named.append((path.name, match.group(1)))
    assert named, "no container command names a felix-* console script — this invariant found nothing"

    for where, binary in named:
        assert binary in scripts, (
            f"{where} runs {binary!r}, which is not a console script in this workspace "
            f"(have: {sorted(scripts)}) — the container would exit 127"
        )


def test_the_api_boots_with_the_arguments_production_passes() -> None:
    """None. Production passes none.

    `create_application()` is what Granian and uvicorn call, and it forwards nothing to
    `create_app()`. Every other caller in this repo — every test, every fixture — passes
    `settings=`, so this is the only place the `settings or get_settings()` branch is taken.

    Calling the real factory rather than `create_app()` directly is the point: it covers the
    one line of `felix_api.main` that turns a request for an ASGI app into this call, and it
    fails on the shape that shipped (reading the optional parameter instead of the resolved
    one) rather than on a paraphrase of it.
    """
    from felix_api.main import create_application

    app = create_application()

    assert app.routes, "the app booted with no routes"
    assert app.state.settings is not None, "create_app() resolved no settings of its own"


@pytest.mark.parametrize("entry", ["main", "scheduler_main", "temporal_main"])
def test_the_worker_entrypoints_import_what_they_will_call(entry: str) -> None:
    """The worker's own module-level dependencies, resolved without starting a broker.

    These functions block forever once called, so what is checked is the import graph behind
    them: the module loads, the attribute exists, and it is callable with no arguments —
    which, as with the API, is how the console script invokes it.
    """
    import inspect

    import felix_worker.main as worker_main

    fn = getattr(worker_main, entry, None)
    assert callable(fn), f"felix-worker's {entry} is gone; the console script would fail at exec"

    required = [
        name
        for name, param in inspect.signature(fn).parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert not required, f"{entry} needs {required}, but a console script calls it with nothing"
