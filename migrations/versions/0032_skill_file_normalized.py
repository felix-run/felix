"""Store a normalized digest of each skill file's text, so a lightly altered copy is still a copy.

Revision ID: 0032_skill_file_normalized
Revises: 0031_skill_version_adopted_from
Create Date: 2026-10-05

An agent's skill save carries an import's lineage when one of its files is a copy of a file of
imported text in the tenant (`library_store.holds_imported_file`). Byte equality alone missed a
copy that only re-spaced, re-cased, swapped Unicode compatibility forms or inserted invisible
characters, and a SKILL.md copied under a new name, whose frontmatter `name:` differs.
`skill_file` gains `normalized_sha256`: the sha256 of the text after NFKC, removing format
characters (category `Cf`), casefolding, collapsing every run of whitespace to one space and
stripping -- over a SKILL.md's body without its frontmatter, over any other text file whole; null
for a binary asset (`skills/copy_rule.py`, which owns this stored format: changing it needs a
migration that re-hashes these rows). The copy rule matches either digest, and the index serves
the normalized lookup as `0028`'s serves the byte one.

No backfill. The bytes live in the object store, not in Postgres, and a migration that reached
into the object store would need its credentials and a pass over every tenant's files inside a
schema change. Existing rows stay null for good -- a version's rows are immutable, and a re-save
writes a new version rather than rewriting this one -- so a version saved before this revision,
an import included, is matched by its byte digest only. A backfill from the object store (a
worker task or a CLI command) is an open item.

The column is nullable, so adding it rewrites nothing. The index is built `CONCURRENTLY`,
outside the migration's transaction, so saves keep writing `skill_file` while it builds. RLS is
unchanged: the table's policy covers every column. The downgrade drops the index and the column,
and the copy rule falls back to bytes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_skill_file_normalized"
down_revision: str | None = "0031_skill_version_adopted_from"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_skill_file_tenant_normalized_sha256"


def upgrade() -> None:
    op.add_column("skill_file", sa.Column("normalized_sha256", sa.Text(), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "skill_file",
            ["tenant_id", "normalized_sha256"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name="skill_file", postgresql_concurrently=True, if_exists=True)
    op.drop_column("skill_file", "normalized_sha256")
