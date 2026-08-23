"""Approval consumption — make ``one_shot`` enforceable.

``ApprovalRule.one_shot`` was declared in the manifest schema but had no storage behind
it, so one human approval authorized unlimited replays of the same call. A grant is now
marked spent when it is used.

Revision ID: 0007_approval_consumed_at
Revises: 0006_tenant_rls
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_approval_consumed_at"
down_revision: str | None = "0006_tenant_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("consumed_at", sa.BigInteger(), nullable=True))
    # Existing approved grants stay usable; they simply have no consumption recorded.
    op.create_index(
        "idx_approvals_lookup",
        "approvals",
        ["tenant_id", "manifest_id", "tool_name", "call_signature", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_approvals_lookup", table_name="approvals")
    op.drop_column("approvals", "consumed_at")
