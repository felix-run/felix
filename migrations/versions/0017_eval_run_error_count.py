"""Let an eval run say how many of its failures never reached the scorer.

Revision ID: 0017_eval_run_error_count
Revises: 0016_attachment_ledger
Create Date: 2026-09-18

`fail_count` counted an item the scorer rejected and an item that raised as the same thing,
so an operator reading a failing run could not tell a model regression from a malformed
dataset. `scripts/eval-counter-smoke.sh` resolved that out of band for CI by grepping the
printed rows for `error`; nothing on the API surface offered the equivalent. `error_count` is
that subset on the row. `fail_count` keeps its meaning — "did not pass" — which the CLI's exit
code and every existing reader rely on.

Server default 0, so every existing row reads as "no errors recorded", which is true of what
they recorded rather than a claim about what happened.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_eval_run_error_count"
down_revision: str | None = "0016_attachment_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("eval_runs", "error_count")
