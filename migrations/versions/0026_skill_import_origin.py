"""Skill imports: where an imported version came from, its lineage, and the cooldown's clock.

Revision ID: 0026_skill_import_origin
Revises: 0025_plan_thread_id
Create Date: 2026-10-03

`skills/importer.py` fetches an Agent Skill from GitHub and saves it as a library draft. Such a
version is neither an agent's nor an operator's own text, so it gets a source of its own,
`import`, and nullable columns naming its origin: the canonical source (`github:owner/repo/path`),
the ref asked for, the commit it resolved to, a digest of the skill folder's kept files at that
commit (a re-import with the same digest saves nothing), the repository's SPDX license, and the
committer date GitHub reports for the folder (provenance only: a pusher sets it).

`skill_version.lineage_import` marks an import and every version built on one, so an edit of
third-party text is still judged and screened as third-party text. False on every existing row:
nothing was imported before this revision.

`skill_import_sighting` is the cooldown's clock: when this tenant first saw a source's files with a
given digest. Tenant-scoped, under the same RLS policy as every other skill table.
`skill_policy.import_min_age_days` is the tenant's side of the cooldown, tighten-only against
`FELIX_SKILL_IMPORT_MIN_AGE_DAYS` as every other field of that row is.

Expand-only, and the added columns are nullable or carry a constant default: catalog-only changes
with no table rewrite. The source check is dropped and re-added in this migration's transaction,
which holds `skill_version` under an ACCESS EXCLUSIVE lock while the new check scans its rows; the
table holds one row per skill version, so that scan is short.

The downgrade narrows the check back, and so refuses while any `import` version exists: dropping
those rows would delete a tenant's review history, and that is a decision for a person, not a
rollback script. Archive or remove imported versions first (`deploy-runbook`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_skill_import_origin"
down_revision: str | None = "0025_plan_thread_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEXT_COLUMNS = ("origin_source", "origin_ref", "origin_commit", "origin_tree_hash", "origin_license")
_CHECK = "ck_skill_version_source"
_SIGHTING = "skill_import_sighting"


def _replace_check(sources: str) -> None:
    op.drop_constraint(_CHECK, "skill_version", type_="check")
    op.create_check_constraint(_CHECK, "skill_version", f"source IN ({sources})")


def upgrade() -> None:
    for column in _TEXT_COLUMNS:
        op.add_column("skill_version", sa.Column(column, sa.Text(), nullable=True))
    op.add_column("skill_version", sa.Column("origin_committed_at", sa.BigInteger(), nullable=True))
    op.add_column(
        "skill_version",
        sa.Column("lineage_import", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    _replace_check("'agent', 'operator', 'import'")
    op.add_column(
        "skill_policy",
        sa.Column("import_min_age_days", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        _SIGHTING,
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("origin_source", sa.Text(), primary_key=True),
        sa.Column("tree_hash", sa.Text(), primary_key=True),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
    )
    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0022`, `0023`).
    op.execute(f'ALTER TABLE "{_SIGHTING}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_SIGHTING}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{_SIGHTING}"')
    op.execute(
        f"""
        CREATE POLICY felix_tenant_isolation ON "{_SIGHTING}"
        USING (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    imported = op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_version WHERE source = 'import'"))
    if imported:
        raise RuntimeError(
            f"{imported} skill versions were imported; archive or remove them before downgrading "
            "past 0026_skill_import_origin"
        )
    op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{_SIGHTING}"')
    op.drop_table(_SIGHTING)
    op.drop_column("skill_policy", "import_min_age_days")
    _replace_check("'agent', 'operator'")
    op.drop_column("skill_version", "lineage_import")
    op.drop_column("skill_version", "origin_committed_at")
    for column in reversed(_TEXT_COLUMNS):
        op.drop_column("skill_version", column)
