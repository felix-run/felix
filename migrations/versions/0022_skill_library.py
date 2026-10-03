"""A per-tenant skill library: skills, their immutable versions, and each version's files.

Revision ID: 0022_skill_library
Revises: 0021_push_subscriptions
Create Date: 2026-10-02

Until now nothing could write a skill: the catalog read the bundled directory,
`FELIX_SKILLS_DIR`, and object-store keys an operator uploaded by hand. These tables are the
review record for skills an agent drafts or an operator authors. The bytes stay in the object
store at `skills/{tenant}/{name}/{version}/{path}` -- the layout `skills/loader.py` already
reads -- so a row here says what a version is and whether it may be loaded, never what it says.

`skill.live_version` is the only thing a catalog follows. A draft, a rejected draft and a
superseded version all keep their rows (versions are immutable, so rollback is a pointer move)
and none of them is ever loaded. Empty on every existing deployment, and nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_skill_library"
down_revision: str | None = "0021_push_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("skill", "skill_version", "skill_file")


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        # Null when the skill is archived: it keeps its history and leaves every catalog.
        sa.Column("live_version", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "skill_version",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        # Semver text. The primary key is what makes two concurrent saves of the same next
        # version collide rather than both landing.
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("parent_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False, server_default=""),
        sa.Column("origin_manifest_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("quality_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("security_status", sa.Text(), nullable=False),
        sa.Column(
            "security_issues",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "review_checks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_skill_version_status"),
        sa.CheckConstraint("source IN ('agent', 'operator')", name="ck_skill_version_source"),
    )
    op.create_table(
        "skill_file",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
    )

    # The pending-draft cap counts one manifest's agent drafts on every save, and the review
    # queue lists a tenant's drafts newest first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_version_pending "
        "ON skill_version (tenant_id, origin_manifest_id) WHERE status = 'draft'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_version_status_age "
        "ON skill_version (tenant_id, status, created_at)"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0021`).
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
        op.execute(
            f"""
            CREATE POLICY felix_tenant_isolation ON "{table}"
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
    for table in _TABLES:
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
    op.execute("DROP INDEX IF EXISTS idx_skill_version_status_age")
    op.execute("DROP INDEX IF EXISTS idx_skill_version_pending")
    op.drop_table("skill_file")
    op.drop_table("skill_version")
    op.drop_table("skill")
