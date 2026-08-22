"""Repo invariants, enforced.

These rules are written in prose in CLAUDE.md and .claude/rules/, which means
they hold only while whoever (or whatever) is editing has them in context. The
tests below make them fail loudly instead — for humans and agents alike.

Each test is structural (AST or file inspection) rather than behavioral, so it
costs nothing at runtime and cannot be satisfied by mocking.
"""

from __future__ import annotations

import ast
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
