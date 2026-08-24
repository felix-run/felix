"""Repo invariants, enforced.

These rules are written in prose in CLAUDE.md and .claude/rules/, which means
they hold only while whoever (or whatever) is editing has them in context. The
tests below make them fail loudly instead — for humans and agents alike.

Each test is structural (AST or file inspection) rather than behavioral, so it
costs nothing at runtime and cannot be satisfied by mocking.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "packages" / "harness" / "src" / "felix"
SOURCE_ROOTS = [
    ROOT / "packages" / "harness" / "src",
    ROOT / "packages" / "cli" / "src",
    ROOT / "apps" / "api" / "src",
    ROOT / "apps" / "worker" / "src",
]

# Distributions that live behind an optional extra in packages/harness/pyproject.toml.
# Importing one at module scope breaks the lean install and the default Docker image.
OPTIONAL_DISTRIBUTIONS = {
    "aiobotocore",
    "boto3",
    "botocore",
    "clickhouse_connect",
    "docker",
    "duckdb",
    "google",
    "mcp",
    "opentelemetry",
    "playwright",
    "polars",
    "presidio_analyzer",
    "presidio_anonymizer",
    "pymysql",
    "sentence_transformers",
    "spacy",
    "temporalio",
}


def _python_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if p.is_file()] if base.is_dir() else []


# --------------------------------------------------------------------------
# Lean by default: optional dependencies are imported inside the function that
# needs them, never at module scope.
# --------------------------------------------------------------------------
def test_no_optional_dependency_imported_at_module_scope() -> None:
    offenders: list[str] = []
    for root in SOURCE_ROOTS:
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in tree.body:  # module scope only — nested imports are the point
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in OPTIONAL_DISTRIBUTIONS:
                        rel = path.relative_to(ROOT)
                        offenders.append(f"{rel}:{node.lineno} imports {name}")
    assert offenders == [], (
        "Optional dependencies must be imported inside the function that needs them, "
        "so the lean install and the default Docker image keep working:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Every module that talks to Postgres also has a memory:// path, which is the
# only way the suite runs without infrastructure.
# --------------------------------------------------------------------------
def test_postgres_modules_have_an_in_memory_path() -> None:
    # db/__init__.py is a pure re-export surface; db/session.py defines the switch.
    exempt = {"db/__init__.py", "db/session.py"}
    offenders: list[str] = []
    for path in _python_files(HARNESS):
        rel = str(path.relative_to(HARNESS))
        if rel in exempt:
            continue
        src = path.read_text(encoding="utf-8")
        if "get_session_factory" not in src:
            continue
        if "_use_memory" not in src and "InMemory" not in src:
            offenders.append(rel)
    assert offenders == [], (
        "These modules reach Postgres with no memory:// fallback, so the test suite "
        "(FELIX_DATABASE_URL=memory://ci) cannot exercise them:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# The governance wrapper stack in build_agent is ordered, and the order is the
# control: each apply_* clones the tool with a new executor, so the sequence
# decides which check sees a call first and which sees the other's output.
# --------------------------------------------------------------------------
EXPECTED_WRAPPER_ORDER = [
    "apply_secret_masking",
    "apply_policies",
    "apply_command_screening",
    "apply_content_screening",
    "apply_limits",
    "apply_guardrails",
    "apply_judges",
    "apply_approvals",
    "apply_artifact_spill",
]


def test_governance_wrapper_order_is_unchanged() -> None:
    builder = HARNESS / "manifests" / "builder.py"
    tree = ast.parse(builder.read_text(encoding="utf-8"), str(builder))

    build_agent = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_agent"
        ),
        None,
    )
    assert build_agent is not None, "build_agent not found in manifests/builder.py"

    # Only the calls that rewrite the tool list itself (`resolved = apply_x(...)`).
    # Other apply_* helpers in build_agent, such as apply_transparency_notice,
    # act on the prompt and are not part of the wrapper stack.
    wrapper_calls = [
        node
        for node in ast.walk(build_agent)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id.startswith("apply_")
        and any(isinstance(target, ast.Name) and target.id == "resolved" for target in node.targets)
    ]
    # ast.walk yields breadth-first; sort by source position for the real sequence.
    applied = [node.value.func.id for node in sorted(wrapper_calls, key=lambda n: (n.lineno, n.col_offset))]
    assert applied == EXPECTED_WRAPPER_ORDER, (
        "The governance wrapper order changed. It is load-bearing: later wrappers are "
        "outermost, so reordering changes which control runs first.\n"
        f"  expected: {EXPECTED_WRAPPER_ORDER}\n"
        f"  found:    {applied}\n"
        "If the change is deliberate, update EXPECTED_WRAPPER_ORDER, the comment in "
        "builder.py, the governance-pipeline skill, and deploy/GOVERNANCE.md together."
    )


# --------------------------------------------------------------------------
# Every FELIX_ setting is discoverable by an operator reading .env.example.
# --------------------------------------------------------------------------
def test_env_example_documents_every_setting() -> None:
    config = (HARNESS / "config.py").read_text(encoding="utf-8")
    body = config.split("class Settings(BaseSettings):", 1)[1].split("\n    def ", 1)[0]
    fields = {
        match.group(1)
        for match in re.finditer(r"^    ([a-z][a-z0-9_]*): ", body, re.MULTILINE)
        if not match.group(1).startswith("model_")
    }
    assert fields, "no Settings fields parsed — has config.py been restructured?"

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8").upper()
    missing = sorted(f for f in fields if f"FELIX_{f.upper()}" not in env_example)

    assert missing == [], (
        "Every FELIX_ setting belongs in .env.example, commented out if it is not part "
        "of a normal deployment, so operators can discover it:\n  "
        + "\n  ".join(f"FELIX_{name.upper()}" for name in missing)
    )


# --------------------------------------------------------------------------
# The editor-facing JSON Schema tracks the pydantic models it is generated from.
# --------------------------------------------------------------------------
def test_manifest_json_schema_is_current() -> None:
    spec = importlib.util.spec_from_file_location(
        "_gen_manifest_schema", ROOT / "scripts" / "gen-manifest-schema.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    checked_in = ROOT / "schemas" / "manifest.schema.json"
    assert checked_in.exists(), (
        "schemas/manifest.schema.json is missing — the bundled manifests point their "
        "yaml-language-server header at it. Run: make schema"
    )
    assert checked_in.read_text(encoding="utf-8") == module.render(), (
        "schemas/manifest.schema.json is stale relative to felix.manifests.schema.Manifest.\n"
        "It is generated, not hand-edited — run: make schema"
    )


def test_wrapping_a_tool_preserves_every_field() -> None:
    """Governance wrappers clone each tool, so a dropped field silently resets to default.

    Every tool passes through `_clone_tool` on its way through the wrapper stack. A
    field-by-field rebuild that forgot one would reset it on every wrapped tool in the
    process, and a default is what a field means when nobody has thought about it —
    `replay_safe` was added and very nearly lost that way. Asserting the round trip here
    means the next field added cannot be dropped quietly.
    """
    import dataclasses

    from felix.manifests.builder import _clone_tool
    from felix.tools.types import Tool, define_tool

    async def _handler(args: dict, ctx: object | None = None) -> str:
        return "ok"

    original = define_tool(
        name="probe",
        description="d",
        handler=_handler,
        source="unit-test",
        fatal=True,
        replay_safe=True,
    )
    clone = _clone_tool(original, original.executor)

    carried = [f.name for f in dataclasses.fields(Tool) if f.name != "executor"]
    assert carried, "Tool has no fields to carry"
    for name in carried:
        assert getattr(clone, name) == getattr(original, name), (
            f"_clone_tool dropped Tool.{name}; wrapped tools would silently use its default"
        )


def test_no_base_http_middleware_anywhere_in_the_source() -> None:
    """BaseHTTPMiddleware is banned in this repo, at the source level.

    `tests/unit/test_middleware_stack.py` asserts the stack `create_app()` builds
    holds none, but that only sees what core wires. A plugin router, a mounted
    sub-app, or an unused factory is invisible to it — and two such factories did
    survive the conversion, each an out-of-date copy of a policy that had been
    rewritten, reachable by anyone who grepped for the old name.

    The cost is measured, not stylistic: ~143us per request per layer, and ~76us per
    streamed token, on an SSE-first harness. Pure-ASGI middleware costs nothing
    measurable. See `felix_api.middleware` for the shape to copy.

    Matched through the AST, not by grep: this file and the modules it polices all
    *describe* BaseHTTPMiddleware in prose, and a text search cannot tell a warning
    about it from a use of it.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []

    for base in ("apps", "packages"):
        for path in (root / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(root)
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.alias):
                    name = node.name.rsplit(".", 1)[-1]
                if name == "BaseHTTPMiddleware":
                    offenders.append(f"{rel}:{getattr(node, 'lineno', '?')}: BaseHTTPMiddleware")
                    continue
                # `@app.middleware("http")` — the decorator form, which builds one.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "middleware"
                    and any(isinstance(a, ast.Constant) and a.value == "http" for a in node.args)
                ):
                    offenders.append(f'{rel}:{node.lineno}: @....middleware("http")')

    assert offenders == [], (
        "BaseHTTPMiddleware / @app.middleware('http') found in source:\n  "
        + "\n  ".join(offenders)
        + "\nWrite pure-ASGI middleware instead — see felix_api.middleware."
    )


def test_every_memory_retirement_route_is_accounted_for() -> None:
    """A memory leaves recall by having its `status` written. Each such route needs a
    trust predicate, and each one was found by a separate review round rather than by
    looking — which is what this replaces.

    The allowlist is the point: a new route fails this test until someone writes down
    which of the two it is. Pointwise enforcement is what made eight rounds necessary;
    an enumeration nobody maintains would make a ninth.
    """
    import ast
    from pathlib import Path

    # function name -> why it is safe to write `status` there
    ACCOUNTED = {
        "_put_in_memory": "guarded: _may_displace / _may_reactivate, then _preserve",
        "_put_in_postgres": "guarded: _refused_in_sql on every preserved column",
        "forget": "guarded: _rank(source) vs _trust(row), stamp only rises",
        "supersede": "guarded: _rank(source) vs _trust(row)",
        "consolidate_pools": (
            "unguarded, and reachable only for rows predating content-hash ids: it "
            "dedupes on exact (tenant, manifest, content), and identical content now "
            "yields an identical id, so two such rows cannot coexist to be merged."
        ),
    }

    src = Path(__file__).resolve().parents[2] / "packages/harness/src/felix/memory/store.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    # Both axes. The module docstring is explicit that `status` and `superseded_seq`
    # must never disagree, and `as_of` filters on `superseded_seq` alone -- so a route
    # writing only that one hides a row from every as-of reconstruction.
    VISIBILITY = {"status", "superseded_seq"}

    def writes_visibility(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant):
                        if t.slice.value in VISIBILITY:
                            return True
                    if isinstance(t, ast.Attribute) and t.attr in VISIBILITY:
                        return True
            if isinstance(child, ast.keyword) and child.arg in VISIBILITY:
                return True
            # `set_={...}` mappings too, but only those -- a bare dict naming
            # "status" is usually a read projection like `_row_dict`. Without this,
            # `_put_in_postgres` entered the set only by way of an incidental
            # `.values(status=...)` in a sibling statement, so refactoring that
            # sibling would have dropped the arm that carried the production-only
            # bug, silently, with the test still green.
            if isinstance(child, ast.keyword) and child.arg == "set_":
                for key in getattr(child.value, "keys", []):
                    if isinstance(key, ast.Constant) and key.value in VISIBILITY:
                        return True
        return False

    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and writes_visibility(node)
    }
    unaccounted = sorted(found - set(ACCOUNTED))
    assert unaccounted == [], (
        f"memory retirement routes with no recorded trust reasoning: {unaccounted}. "
        "Add the predicate, or record in ACCOUNTED why the route cannot be reached "
        "by an untrusted writer."
    )

    # A floor, so a route *leaving* the set fails as loudly as one entering it. A
    # detector that can silently stop seeing a route has the same defect as the
    # pointwise guards it replaced -- it infers safety from an absent signal.
    missing = sorted({"_put_in_memory", "_put_in_postgres", "forget", "supersede"} - found)
    assert missing == [], (
        f"routes that should write visibility are no longer detected: {missing}. "
        "Either they stopped writing it, or the detector stopped seeing them."
    )
