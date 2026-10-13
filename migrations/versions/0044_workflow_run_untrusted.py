"""Record which of a workflow run's inputs came from a third party.

Revision ID: 0044_workflow_run_untrusted
Revises: 0043_job_workflow
Create Date: 2026-10-13

A signed trigger can start a workflow run with inputs mapped from the sender's payload
(`felix.triggers`). A string from there is a third party's text, and an agent step must read it
as data, not instructions: `untrusted_json` lists those inputs, and while it is non-empty every
value an agent step's prompt is filled with is fenced as data (`felix.workflows.kinds`).

Expand-only: the column defaults to `[]`, which is every existing run, filled as before. RLS is
unchanged: the table's policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_workflow_run_untrusted"
down_revision: str | None = "0043_job_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "untrusted_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "untrusted_json")
