"""Record who created a workflow, and through what.

Revision ID: 0045_workflow_owner
Revises: 0044_workflow_run_untrusted
Create Date: 2026-10-22

A workflow belongs to its tenant, and `updated_by` names only its last writer, so nothing said
who a workflow was made by. An agent writing workflows for the person it acts for
(`spec.workflow_authoring`) may change only the ones it made for that person, never an operator's:
`created_by` is the creating subject and `authored_via` what created it (`agent:<manifest>`, or
empty for the routes). Both are set when the row is created and kept on every replace.

Expand-only: both columns default to `''`, which is every existing row -- a workflow made through
the routes, by a writer nobody recorded. RLS is unchanged: the table's policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_workflow_owner"
down_revision: str | None = "0044_workflow_run_untrusted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("created_by", sa.Text(), server_default="", nullable=False))
    op.add_column("workflows", sa.Column("authored_via", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("workflows", "authored_via")
    op.drop_column("workflows", "created_by")
