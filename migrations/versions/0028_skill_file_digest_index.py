"""Index a tenant's skill files by digest.

Revision ID: 0028_skill_file_digest_index
Revises: 0027_github_connections
Create Date: 2026-10-04

An agent's skill save asks whether any of its files is byte-for-byte a file of an imported skill
anywhere in the tenant (`library_store.holds_imported_file`), so a copy of third-party text carries
the import's lineage. That is a lookup of `skill_file` by `(tenant_id, sha256)`, which the primary
key `(tenant_id, name, version, path)` cannot serve: without this index each agent save scans the
tenant's files.

Expand-only and empty of meaning on its own. Built `CONCURRENTLY` -- outside the migration's
transaction, which `CREATE INDEX CONCURRENTLY` refuses to run inside -- so saves keep writing
`skill_file` while it builds.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_skill_file_digest_index"
down_revision: str | None = "0027_github_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_skill_file_tenant_sha256"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX, "skill_file", ["tenant_id", "sha256"], postgresql_concurrently=True, if_not_exists=True
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name="skill_file", postgresql_concurrently=True, if_exists=True)
