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
# Modules that may import an optional dependency at module scope, because the
# dependency requires it and nothing reaches them without the extra installed.
#
# One entry, and it earns it: `@workflow.run` rejects a class declared inside a
# function -- the Temporal worker re-imports the class by name inside its sandbox --
# so the definitions cannot be built lazily the way every other optional binding is.
#
# The carve-out is enforced rather than trusted. `test_an_extra_only_module_is_never
# _imported_eagerly` below asserts nothing pulls these in at module scope, which is
# the property that makes a module-scope `import temporalio` harmless here.
EXTRA_ONLY_MODULES = {"packages/harness/src/felix/durability/_temporal_workflow.py"}


def test_every_extra_only_module_exists() -> None:
    """So the list cannot outlive the file it excuses."""
    missing = sorted(rel for rel in EXTRA_ONLY_MODULES if not (ROOT / rel).is_file())
    assert missing == [], f"EXTRA_ONLY_MODULES names files that no longer exist: {missing}"


def test_an_extra_only_module_is_never_imported_eagerly() -> None:
    """The whole basis of the exception.

    A module-scope `import temporalio` is harmless only while nothing imports the
    module that does it at *its* module scope. The moment something does, a lean
    install breaks at import time — which is exactly what the rule below exists to
    prevent, so the exception has to carry its own guard.
    """
    targets = {Path(rel).stem for rel in EXTRA_ONLY_MODULES}
    offenders: list[str] = []
    for root in SOURCE_ROOTS:
        for path in _python_files(root):
            if str(path.relative_to(ROOT)) in EXTRA_ONLY_MODULES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in tree.body:  # module scope only
                mod = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                elif isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                if any(t in mod.split(".") or t in mod.split(",") for t in targets):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {mod}")
    assert offenders == [], (
        "An extra-only module must be imported inside the function that needs it, or a "
        "lean install fails at import:\n  " + "\n  ".join(offenders)
    )


# Lean by default: optional dependencies are imported inside the function that
# needs them, never at module scope.
# --------------------------------------------------------------------------
def test_no_optional_dependency_imported_at_module_scope() -> None:
    offenders: list[str] = []
    for root in SOURCE_ROOTS:
        for path in _python_files(root):
            if str(path.relative_to(ROOT)) in EXTRA_ONLY_MODULES:
                continue
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


def test_every_memory_store_function_is_classified() -> None:
    """Every top-level function in the memory store is either a retirement route with
    a stated predicate, or explicitly not one.

    The previous shape asked "did the detector see this idiom", which has a silent-miss
    mode: a route written as `.values(**payload)` or `set_=mapping_var` enters neither
    the found set nor the unaccounted set, and passes with no entry at all. That is the
    same defect as the pointwise guards it replaced — inferring safety from an absent
    signal — one level up.

    This asks "did someone classify this function", which no idiom can dodge. The cost
    is one line per new helper. That is the price of the guarantee.
    """
    import ast
    from pathlib import Path

    # Routes that can take a memory out of recall, and the predicate that guards each.
    RETIREMENT = {
        "_put_in_memory": "guarded: _may_displace / _may_reactivate, then _preserve",
        "_put_in_postgres": "guarded: _refused_in_sql on every preserved column",
        "forget": "guarded: _rank(source) vs _trust(row), _retirer_rank stamp only rises",
        "supersede": "guarded: _rank(source) vs max(_trust, _retirer_rank), stamps retirer",
        "put_memory": (
            "delegates to _put_in_memory / _put_in_postgres, which apply _may_displace "
            "and _refused_in_sql; names its writer when it calls supersede"
        ),
        "consolidate_pools": (
            "unguarded, and unreachable: it dedupes on exact (tenant, manifest, content), "
            "and identical content now yields an identical id, so two such rows cannot "
            "coexist to be merged. Precondition for any future writer: ids stay content-derived."
        ),
    }
    # Everything else: reads, projections, ranking helpers, embedding plumbing. Listed
    # explicitly rather than inferred, which is the whole point -- a new function is
    # unclassified until someone says which it is.
    NOT_RETIREMENT = {
        "now_ms",
        "memory_id",
        "_row_dict",
        "_is_active",
        "current_turn_seq",
        "_trust_of_column",
        "_rank",
        "trust_of",
        "_retirer_rank_of_column",
        "_stamp_retirer",
        "_retirer_rank",
        "_trust",
        "_preserve",
        "_may_reactivate",
        "_may_displace",
        "_refused_in_sql",
        "_write_embedding",
        "_configured_dim",
        "get_many",
        "list_active",
        "as_of",
    }

    src = Path(__file__).resolve().parents[2] / "packages/harness/src/felix/memory/store.py"
    module_src = src.read_text(encoding="utf-8")
    tree = ast.parse(module_src)

    # This verifies that a reason cites something real. It does **not** verify that the
    # reason is accurate: `supersede`'s entry went stale inside one commit because the
    # predicate changed from `_trust(row)` to `max(_trust(row), _retirer_rank(row))`,
    # and both symbols still existed, so a check like this would have passed. Judging
    # whether a stated predicate still describes the code -- and whether a reachability
    # argument still holds -- is a review obligation this test cannot discharge.
    #
    # What it does buy: a reason cannot cite a symbol that has been deleted or renamed,
    # and every entry must cite something, so an entry cannot quietly become prose.
    TRACKED = (
        "_may_displace",
        "_may_reactivate",
        "_refused_in_sql",
        "_preserve",
        "_retirer_rank",
        "_rank",
        "_trust",
        "content-derived",
    )
    for name, reason in RETIREMENT.items():
        cited = [sym for sym in TRACKED if sym in reason]
        assert cited, (
            f"RETIREMENT[{name!r}] cites no tracked symbol, so nothing anchors its reason "
            "to the code. Name the predicate, or the precondition it depends on."
        )
        for symbol in cited:
            if symbol.startswith("_"):
                assert symbol in module_src, (
                    f"RETIREMENT[{name!r}] cites {symbol}, which no longer exists in the module"
                )

    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    classified = set(RETIREMENT) | NOT_RETIREMENT
    unclassified = sorted(defined - classified)
    assert unclassified == [], (
        f"unclassified functions in the memory store: {unclassified}. Add each to "
        "RETIREMENT with the predicate that guards it, or to NOT_RETIREMENT."
    )
    # And the reverse, so a classification cannot outlive the function it describes.
    stale = sorted(classified - defined)
    assert stale == [], f"classifications for functions that no longer exist: {stale}"

    # The classification above answers "did someone label this function" and never
    # rechecks the label; the detector below answers "does this function write
    # visibility" and can miss a function the walk does not reach. Neither is
    # sufficient alone -- classification misses a mislabelled function, detection
    # misses an unreached one -- so both run and each covers the other's blind spot.
    # Same lesson as trusting one store arm.
    VISIBILITY = {"status", "superseded_seq"}

    def writes_visibility(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                        if target.slice.value in VISIBILITY:
                            return True
                    if isinstance(target, ast.Attribute) and target.attr in VISIBILITY:
                        return True
            if isinstance(child, ast.keyword):
                if child.arg in VISIBILITY:
                    return True
                if child.arg == "set_":
                    for key in getattr(child.value, "keys", []):
                        if isinstance(key, ast.Constant) and key.value in VISIBILITY:
                            return True
        return False

    # `ast.walk`, not `tree.body`: a method on a class is invisible to the latter, and
    # an additive class is the likelier change -- a wholesale refactor would already
    # trip the `stale` assertion above.
    detected = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and writes_visibility(node)
    }
    mislabelled = sorted(detected & NOT_RETIREMENT)
    assert mislabelled == [], f"classified as not-retirement but writes visibility: {mislabelled}"
    undeclared = sorted(detected - set(RETIREMENT))
    assert undeclared == [], f"writes visibility with no retirement classification: {undeclared}"


def test_ci_installs_every_extra_the_tests_gate_on() -> None:
    """An extras-gated test module must be one CI actually installs the extra for.

    `tests/unit/test_temporal_backend.py` gated six tests on `temporalio`, which lives
    behind the `temporal` extra — and the CI test job installed `--dev` only. Those tests
    never ran in CI, and because a module-level `importorskip` collapses to one
    collect-time skip they never appeared in the skip count either: the run simply
    reported fewer tests. The most recent Temporal change shipped with its tests
    unexecuted.

    Adding a new gate is fine; adding one CI cannot satisfy is what this catches.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # Anchored to the job that runs the suite, not the first `uv sync` in the file: a new
    # job added above it with its own `--extra` would otherwise silently become the thing
    # this invariant reads, and it would pass while asserting about the wrong install.
    lines = workflow.splitlines()
    runs_suite = next(
        (i for i, line in enumerate(lines) if "./scripts/test.sh" in line and "--cov" in line), None
    )
    assert runs_suite is not None, "no CI step runs the suite with coverage — has ci.yml moved?"
    install = next(
        (line for line in reversed(lines[:runs_suite]) if "uv sync" in line),
        "",
    )
    # `--all-extras` satisfies every gate, so the check is vacuous rather than wrong there.
    if "--all-extras" in install:
        return
    installed = set(re.findall(r"--extra\s+([A-Za-z0-9_-]+)", install))

    gated: set[str] = set()
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "require_optional"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
            ):
                gated.add(str(node.args[1].value))

    assert gated, "no require_optional() gates found — has the helper been renamed?"
    missing = sorted(gated - installed)
    assert missing == [], (
        f"tests gate on extras the CI test job does not install: {missing}. "
        f"Add `--extra {' --extra '.join(missing)}` to the Pytest job's uv sync, "
        f"or the tests behind them will silently not run."
    )


def test_optional_extras_are_gated_through_the_helper() -> None:
    """A bare `importorskip` bypasses the CI requirement flag, so it must not come back."""
    helper = ROOT / "tests" / "optional_deps.py"  # where the one legitimate call lives
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        if path == helper:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "importorskip"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], (
        "use tests/optional_deps.py:require_optional(module, extra) instead of "
        f"pytest.importorskip so CI can require the extra: {offenders}"
    )


# The governance wrappers whose config comes straight from the manifest schema.
#
# Derived from EXPECTED_WRAPPER_ORDER rather than hand-written. A hand-written list was a
# second, partial copy of the stack: it named six of the nine wrappers, so the same
# fail-open shape could ship in `apply_secret_masking`, `apply_policies` or
# `apply_approvals` with both invariants below still green — a vacuous gate, which is the
# failure this file exists to prevent. Adding a tenth wrapper now forces it into
# EXPECTED_WRAPPER_ORDER (test_governance_wrapper_order_is_unchanged fails otherwise) and
# it is checked here for free.
#
# `apply_artifact_spill` is excluded because it lives in felix/artifacts.py, not
# builder.py; the intersection below drops it, and test_governance_wrappers_all_resolve
# asserts the set is not silently empty.
GOVERNANCE_WRAPPERS = set(EXPECTED_WRAPPER_ORDER) | {"wrap_final_response_judges"}

# Parameters that are plumbing rather than manifest config. Deliberately short: an
# earlier version also excluded `secrets`, `policies` and `rules` — which are exactly the
# parameters of the three wrappers the hand-written set had been missing, so the exclusion
# recreated the same blind spot by another route. They are already typed `list[str]`,
# `list[Policy]` and `list[ApprovalRule]`, so they need no exemption to pass.
_NON_CONFIG_PARAMS = {"self", "tools", "agent", "manifest_id"}


def _builder_wrappers() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The governance wrappers actually defined in builder.py."""
    builder = HARNESS / "manifests" / "builder.py"
    tree = ast.parse(builder.read_text(encoding="utf-8"), str(builder))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in GOVERNANCE_WRAPPERS
    ]


def test_governance_wrappers_all_resolve() -> None:
    """Without this, renaming a wrapper turns both invariants below into vacuous passes."""
    found = {n.name for n in _builder_wrappers()}
    missing = sorted(GOVERNANCE_WRAPPERS - found - {"apply_artifact_spill"})
    assert missing == [], (
        f"GOVERNANCE_WRAPPERS names functions not defined in builder.py: {missing}. "
        "Renamed or moved? Update the set, or the checks below stop covering them."
    )
    assert len(found) >= 8, f"expected the full wrapper stack, found only {sorted(found)}"


def test_governance_wrappers_read_their_config_as_typed_attributes() -> None:
    """No `getattr(config, "field", default)` in the wrappers that enforce the manifest.

    `CommandScreening`, `ContentScreening`, `Limits` and `Guardrails` are strict pydantic
    models, but the wrappers took them as `Any` and read every field through a `getattr`
    default. That wrote each default twice — once in the schema, once at the read site,
    free to disagree — and made a renamed field fail *open*: `getattr(screening,
    "enabled", False)` on a model that no longer has `enabled` disables screening
    silently. The `Guardrails.providers` comment records the same shape shipping once
    already, as a typo that meant no wrapper was applied while `guardrails_enabled()`
    still returned True.

    Reading the fields as attributes is what lets `ty` see this layer at all.
    """
    offenders: list[str] = []
    for node in _builder_wrappers():
        params = {a.arg for a in (*node.args.args, *node.args.kwonlyargs)}
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "getattr"
                and len(call.args) >= 2
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id in params
            ):
                offenders.append(f"{node.name}:{call.lineno} getattr({call.args[0].id}, ...)")

    assert offenders == [], (
        f"governance config must be read as typed attributes, not getattr defaults: {offenders}"
    )


def test_governance_wrappers_declare_their_config_type() -> None:
    """`Any` here is what let the getattr defaults hide. Keep the annotations concrete."""
    untyped: list[str] = []
    for node in _builder_wrappers():
        for arg in (*node.args.args, *node.args.kwonlyargs):
            if arg.arg in _NON_CONFIG_PARAMS:
                continue
            rendered = ast.unparse(arg.annotation) if arg.annotation else "<none>"
            if "Any" in rendered or rendered == "<none>":
                untyped.append(f"{node.name}({arg.arg}: {rendered})")

    assert untyped == [], f"governance wrapper config parameters must not be Any: {untyped}"


# `model.py` holds the clients themselves plus the fallback/escalation composites: they
# *are* the call, and metering is the caller's job. Every other module under patterns/ is
# a caller.
_UNMETERED_BY_DESIGN = {"model.py"}


def test_a_pattern_that_reaches_a_model_records_the_usage() -> None:
    """Structural, because per-pattern tests cannot cover the next pattern someone adds.

    This defect has now shipped twice. `patterns/model.py:stream_turn` records the first:
    a streamed turn followed by a second `chat()` for the real answer billed the input
    twice and metered only one, so `limits.max_cost_usd` counted roughly half of what a
    streaming run spent. Then `_stream_parallel` and `_stream_plan_execute` never called
    `record_usage` at all, so streamed runs of those patterns were entirely unbilled and
    escaped the token and cost budgets.

    `record_usage` is the sole feed for `ctx.limit_state`, which `limits.check_budgets`
    reads — so a model call that misses it is not merely absent from the usage table, it
    is invisible to the declared spend ceiling. A test per pattern would not have caught
    either instance in the pattern that came next.
    """
    patterns = HARNESS / "patterns"
    offenders: list[str] = []

    for path in sorted(patterns.glob("*.py")):
        if path.name in _UNMETERED_BY_DESIGN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            reaches_model = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in {"chat", "stream_turn"}
                for call in ast.walk(node)
            )
            if not reaches_model:
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "record_usage" not in names:
                offenders.append(f"{path.name}:{node.lineno} {node.name}")

    assert offenders == [], (
        "these call a model without recording usage, so the spend they cause escapes "
        f"limits.max_cost_usd and the token budgets: {offenders}. Call record_usage on the "
        "ModelChatResult, or route the call through a helper that does."
    )


def _self_attrs_touched(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names like `_plugins` from `self._plugins.append(...)` or `self._x = y`."""
    return {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr.startswith("_")
    }


def _identifiers(tree: ast.AST) -> set[str]:
    """Every name a module actually *uses* — not text in comments or string literals.

    Matching raw file text made the check satisfiable by coincidence: a dead
    `register_router` passed on the `_router` inside `include_router`.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
    return names


def test_every_plugin_registration_method_has_a_consumer() -> None:
    """A `register_*` seam nobody reads is worse than a missing one.

    `register_authenticator`, `register_router`, and `register_audit_sink` all
    accepted registrations that core never consulted, so a plugin following the
    documented Protocol got no error and no effect.

    Rather than guess at names, this resolves the chain: each `register_*` method
    writes some `self._field`; a public member of `PluginRegistry` reads that field
    and is the accessor; core must reference the accessor or the field itself.
    """
    plugins_py = HARNESS / "plugins.py"
    tree = ast.parse(plugins_py.read_text(encoding="utf-8"))

    registry = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "PluginRegistry"),
        None,
    )
    assert registry is not None, "PluginRegistry not found"

    methods = [n for n in registry.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    registrars = [m for m in methods if m.name.startswith("register_")]
    assert registrars, "PluginRegistry exposes no register_* methods"

    used: set[str] = set()
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            if path == plugins_py:
                continue
            used |= _identifiers(ast.parse(path.read_text(encoding="utf-8")))

    orphans: list[str] = []
    for method in registrars:
        fields = _self_attrs_touched(method)
        accessors = {
            m.name
            for m in methods
            if not m.name.startswith(("_", "register_")) and (_self_attrs_touched(m) & fields)
        }
        # A hook forwarded straight to another module (register_before_turn and
        # friends) stores nothing here; its consumer is that module's runner.
        if not fields:
            continue
        if not ({method.name, *accessors, *fields} & used):
            orphans.append(f"{method.name} (writes {', '.join(sorted(fields))})")

    assert orphans == [], (
        "plugin registration methods whose registration nothing in core reads — "
        f"wire them or delete them: {'; '.join(orphans)}"
    )


def test_no_tenant_scoped_accessor_defaults_to_the_default_tenant() -> None:
    """A `tenant_id` that defaults is a cross-tenant read waiting to happen.

    `_provenance` called `get_session_store(settings)` and silently read tenant
    "default"'s session log for every caller. That is the third time this class has
    landed — `_announce` and `get_session_store`'s storage half preceded it, both
    recorded in docs/ROADMAP.md — so the rule is enforced rather than remembered.

    Scoped to the session layer, where the accessors hand back a whole tenant's log.
    """
    session_dir = HARNESS / "session"
    offenders: list[str] = []

    for path in sorted(session_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            # Defaults align to the tail of their own parameter list.
            pairs = list(zip(args.args[len(args.args) - len(args.defaults) :], args.defaults, strict=True))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None]
            for arg, default in pairs:
                if arg.arg != "tenant_id":
                    continue
                if isinstance(default, ast.Constant) and default.value == "default":
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno} {node.name}")

    assert offenders == [], (
        "tenant_id must not default to 'default' on a session accessor — omitting it "
        f"should be a TypeError, not another tenant's log: {'; '.join(offenders)}"
    )


def test_no_outbound_http_client_hardcodes_its_timeout() -> None:
    """Every `httpx.AsyncClient(timeout=...)` on a configurable path takes a computed value.

    Six client sites in `patterns/model.py` shared a hardcoded `timeout=120.0`, so no
    deployment could raise the ceiling without editing the harness — and a generation that
    legitimately needed longer failed the whole run. A source-text check would pass the
    moment someone adds a seventh site with a fresh literal, so walk the tree instead: a
    `timeout=` that is a bare constant is the defect.
    """
    configurable = (
        ROOT / "packages/harness/src/felix/patterns/model.py",
        ROOT / "packages/harness/src/felix/mcp/client.py",
        ROOT / "packages/harness/src/felix/a2a/peers.py",
    )
    offenders: list[str] = []
    for path in configurable:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "AsyncClient"):
                continue
            for kw in node.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno} timeout={kw.value.value!r}")

    assert offenders == [], (
        "an outbound client hardcodes its timeout; read it from Settings or a per-ref "
        f"field so an operator can raise it: {'; '.join(offenders)}"
    )
