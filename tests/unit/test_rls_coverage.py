"""Every tenant table is covered by the RLS policy, and every table is a tenant table.

Migration `0006_tenant_rls` applied `felix_tenant_isolation` to a fixed list; a table added
later is covered only if whoever added it remembered, and the failure is silent — the
table simply is not isolated. `document_chunks` (`0010`) carries its policy because it
was written by hand, which is the argument rather than the reassurance.

The migrations are rendered offline (Alembic `as_sql`, no database) in chain order and
the emitted DDL is read back, so what is checked is what a deployment actually runs —
not a tuple in one migration's module. A `tenant_id` column is what makes a table a
tenant table. "Covered" is all three of ENABLE, FORCE and a policy whose predicate
compares `tenant_id` to the session GUC: `FORCE` without `ENABLE` filters nothing, and a
policy `USING (true)` — or a second permissive policy beside the real one, since
permissive policies are OR'd — is present and wrong.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from felix.db.models import Base

ROOT = Path(__file__).resolve().parents[2]
POLICY = "felix_tenant_isolation"
TENANT_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)"
BYPASS_PREDICATE = "current_setting('app.rls_bypass', true) = 'on'"

# Tables allowed to exist without a tenant column — a written decision that their rows
# belong to no tenant. `memory_vector_config` is one deployment-wide row recording the
# dimension the vector column was built at (`0009`); it has no model, is read by raw
# SQL, and never has RLS enabled. `oauth_token_cache` was the other, never read or
# written, and is dropped in `0013`.
TENANTLESS_TABLES: frozenset[str] = frozenset({"memory_vector_config"})

# An optionally schema-qualified, optionally quoted identifier; group 1 is the bare name.
_IDENT = r'(?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'


def _scripts() -> ScriptDirectory:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def _rendered_upgrade_sql() -> str:
    """Every revision's `upgrade()`, base to head, as the SQL Postgres would receive."""
    buf = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf})
    for script in reversed(list(_scripts().walk_revisions())):
        with Operations.context(ctx):
            script.module.upgrade()
    return buf.getvalue()


@dataclass
class SchemaState:
    """What exists after every migration ran, as read back from the DDL."""

    exists: set[str] = field(default_factory=set)
    enabled: set[str] = field(default_factory=set)
    forced: set[str] = field(default_factory=set)
    policy: set[str] = field(default_factory=set)
    #: Table -> the full text of every CREATE POLICY statement still in force on it.
    policies: dict[str, dict[str, str]] = field(default_factory=dict)


def _final_state(ddl: str) -> SchemaState:
    events: list[tuple[int, str, str, str, str]] = []

    def scan(kind: str, pattern: str, *, body: bool = False) -> None:
        """Record (position, kind, name, table, statement): `name` is group 1 — a table,
        or a policy — and `table` is the policy's table when the pattern has a group 2."""
        for m in re.finditer(pattern, ddl, re.IGNORECASE):
            text = ddl[m.start() : ddl.index(";", m.start())] if body else ""
            table = m.group(2) if m.re.groups > 1 else m.group(1)
            events.append((m.start(), kind, m.group(1), table, text))

    scan("create", rf"CREATE TABLE (?:IF NOT EXISTS )?{_IDENT}")
    scan("drop", rf"DROP TABLE (?:IF EXISTS )?{_IDENT}")
    scan("enable", rf"ALTER TABLE {_IDENT} ENABLE ROW LEVEL SECURITY")
    scan("disable", rf"ALTER TABLE {_IDENT} DISABLE ROW LEVEL SECURITY")
    scan("force", rf"ALTER TABLE {_IDENT} FORCE ROW LEVEL SECURITY")
    scan("unforce", rf"ALTER TABLE {_IDENT} NO FORCE ROW LEVEL SECURITY")
    scan("policy", rf"CREATE POLICY ([A-Za-z_][A-Za-z0-9_]*) ON {_IDENT}", body=True)
    scan("unpolicy", rf"DROP POLICY (?:IF EXISTS )?([A-Za-z_][A-Za-z0-9_]*) ON {_IDENT}")

    state = SchemaState()
    for _, kind, name, table, text in sorted(events):
        if kind == "create":
            state.exists.add(name)
        elif kind == "drop":
            state.exists.discard(name)
            state.enabled.discard(name)
            state.forced.discard(name)
            state.policies.pop(name, None)
        elif kind == "enable":
            state.enabled.add(name)
        elif kind == "disable":
            state.enabled.discard(name)
        elif kind == "force":
            state.forced.add(name)
        elif kind == "unforce":
            state.forced.discard(name)
        elif kind == "policy":
            state.policies.setdefault(table, {})[name] = text
        elif kind == "unpolicy":
            state.policies.get(table, {}).pop(name, None)
    state.policy = {table for table, named in state.policies.items() if POLICY in named}
    return state


@pytest.fixture(scope="module")
def state() -> SchemaState:
    return _final_state(_rendered_upgrade_sql())


def _tenant_tables() -> set[str]:
    return {t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.columns}


def test_every_tenant_table_is_enabled_forced_and_carries_the_policy(state: SchemaState) -> None:
    tenant_tables = _tenant_tables()
    assert tenant_tables <= state.exists, (
        f"models without a migration: {sorted(tenant_tables - state.exists)}"
    )
    assert tenant_tables - state.policy == set(), (
        f"tenant tables with no {POLICY} policy: {sorted(tenant_tables - state.policy)} — "
        "add ENABLE + FORCE ROW LEVEL SECURITY and the policy in the migration that creates the table"
    )
    assert tenant_tables - state.enabled == set(), (
        f"policy on a table where RLS is not ENABLEd, so it filters nothing: {sorted(tenant_tables - state.enabled)}"
    )
    assert tenant_tables - state.forced == set(), (
        f"policy without FORCE, so the table owner bypasses it: {sorted(tenant_tables - state.forced)}"
    )


def test_the_policy_predicate_is_the_tenant_guc_and_nothing_else_is_permitted(state: SchemaState) -> None:
    """Present-but-wrong is the shape to fear: `USING (true)`, the wrong column, or a
    second permissive policy beside the real one (permissive policies are OR'd)."""
    for table in sorted(_tenant_tables()):
        named = state.policies.get(table, {})
        assert set(named) == {POLICY}, (
            f"{table}: policies {sorted(named)} — only {POLICY} may exist on a tenant table"
        )
        text = named[POLICY]
        using = re.search(r"USING\s*\((.*?)\)\s*WITH CHECK\s*\((.*?)\)\s*$", text, re.DOTALL | re.IGNORECASE)
        assert using, f"{table}: policy has no USING + WITH CHECK pair:\n{text}"
        for clause in using.groups():
            assert TENANT_PREDICATE in clause, (
                f"{table}: clause does not compare tenant_id to the GUC:\n{clause}"
            )
            assert BYPASS_PREDICATE in clause, (
                f"{table}: clause has no bypass arm, so retention cannot sweep it:\n{clause}"
            )


def test_every_table_is_a_tenant_table(state: SchemaState) -> None:
    """A row that belongs to no tenant is a row every tenant can reach under bypass and no
    tenant can reach under the policy; either way it is a decision, so it is written down.
    Checked against the DDL, not the models: a table created by raw SQL has no model."""
    tenantless = state.exists - _tenant_tables()
    assert tenantless == TENANTLESS_TABLES, sorted(tenantless ^ TENANTLESS_TABLES)
    model_tables = {t.name for t in Base.metadata.sorted_tables}
    assert model_tables <= state.exists, f"models without a migration: {sorted(model_tables - state.exists)}"


def test_the_policy_is_never_on_a_table_without_a_tenant_column(state: SchemaState) -> None:
    """The policy compares `tenant_id`; on a table without one it would refuse every row."""
    assert state.policy - _tenant_tables() == set(), sorted(state.policy - _tenant_tables())


def test_the_migration_chain_has_one_head() -> None:
    """Two branches each adding an `00NN_` revision merge cleanly in git and refuse to run in
    Alembic — which is what this caught when `0012_drop_oauth_token_cache` met
    `0012_fiber_attempts` on main."""
    heads = _scripts().get_heads()
    assert len(heads) == 1, heads
