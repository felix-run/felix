"""Usage events carry their cost and the wire model id.

Revision ID: 0011_usage_cost
Revises: 0010_documents
Create Date: 2026-09-04

`record_tokens` wrote token counts and the *logical* route name. Cost was never stored,
and the stored id is one the price table does not know, so anything recomputing spend
later — `GET /usage`, a report, a bill — got `$0` for every custom route. Cost is now
priced at write time (the only moment the wire id, the rates and any manifest price
override are all in hand) and stored beside the id it was priced by.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_usage_cost"
down_revision: str | None = "0010_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("wire_model_id", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "usage_events",
        sa.Column("cost_usd", sa.Numeric(14, 8), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("usage_events", "cost_usd")
    op.drop_column("usage_events", "wire_model_id")
