"""Record which import-lineage version an operator adopted.

Revision ID: 0031_skill_version_adopted_from
Revises: 0030_skill_update_notify
Create Date: 2026-10-05

`POST /skill-library/{name}/versions/{version}/adopt` (`skills/library.py:adopt`) saves a new
operator draft whose files are byte-identical to an import-lineage version's, without
`lineage_import`: the one save that clears the mark, and only forward. `adopted_from` names the
version it vouched for, beside the row's own `author` (who) and `reason` (why). The copy rule
(`library_store.holds_imported_file`) still counts an adopted version's files as imported text, so
it reads this column too; the `(tenant_id, sha256)` index from `0028` still serves that lookup,
which joins back to the version row by primary key.

Nullable and null on every existing row -- nothing was adopted before this revision -- so adding it
rewrites nothing. RLS is unchanged: the table's policy covers every column. The downgrade drops the
column: an adopted version keeps `lineage_import` false and loses the record of what it adopted,
and the copy rule then catches a copy of its files only through the version it was adopted from,
which holds the same bytes and is never deleted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_skill_version_adopted_from"
down_revision: str | None = "0030_skill_update_notify"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("skill_version", sa.Column("adopted_from", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_version", "adopted_from")
