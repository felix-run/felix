"""Drop `skill_activation`: which skills are active is the thread's, read from its log.

Revision ID: 0047_drop_skill_activation
Revises: 0046_memory_principal
Create Date: 2026-10-28

The table held one set of active skills per `(tenant, manifest)`, shared by every caller and every
conversation of that manifest: `list_skills` said a skill was active in a thread whose model had
never been handed it. A skill is now active in a thread when the thread's session log holds its
`activate_skill` result (`felix.skills.activation`), so the rows are state nothing reads.

Nothing is migrated: the rows describe no conversation in particular. During a rolling deploy an
API process still on the previous release fails `activate_skill` and `list_skills` until it is
replaced (the tool answers an error and the turn goes on); the new release never touches the table.

Downgrade recreates it empty, with the tenant policy `0006_tenant_rls` gave it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_drop_skill_activation"
down_revision: str | None = "0046_memory_principal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("skill_activation")


def downgrade() -> None:
    op.create_table(
        "skill_activation",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), nullable=False),
        sa.Column(
            "active_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "manifest_id"),
    )
    op.execute('ALTER TABLE "skill_activation" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "skill_activation" FORCE ROW LEVEL SECURITY')
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON "skill_activation"
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
