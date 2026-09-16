"""Say why an approval fired, and which call it is blocking.

Revision ID: 0015_approval_reason_and_call
Revises: 0014_approval_thread_id
Create Date: 2026-09-12

`GET /approvals` is the only channel a durable run has — the `approval_required` side event
is an in-process queue, so the frame cannot cross from the worker to the API's stream. Two
fields the frame carries have never been on the row behind it, and both sides of the wire
had already written that down:

- `manifests/builder.py` at the emit: the rule's `description` "reached no client by any
  route: the `/approvals` row does not carry it either."
- `@felix/client`'s `PendingApproval.reason`: "**Frame-only** … the `/approvals` row carries
  no reason at all, so an approval the poll found has none to show."

So an operator who found a waiting approval by polling was shown a tool name and a rule id
and no statement of why the gate exists — while `description`, the one field in
`ApprovalRule` written to be read by a person, sat unread. `tool_call_id` is the same shape
of gap: it is what correlates the prompt with the tool card it is blocking, and without it a
polled approval cannot be attached to anything on screen.

Expand-only, and empty on every historical row. `''` is the harness saying it has no answer
rather than a missing value — a command-screening gate has no rule description, and a gated
tool called outside a tool loop has no call id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_approval_reason_and_call"
down_revision: str | None = "0014_approval_thread_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("reason", sa.Text(), server_default="", nullable=False))
    op.add_column("approvals", sa.Column("tool_call_id", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("approvals", "tool_call_id")
    op.drop_column("approvals", "reason")
