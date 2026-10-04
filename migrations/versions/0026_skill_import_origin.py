"""Record where an imported skill version came from.

Revision ID: 0026_skill_import_origin
Revises: 0025_plan_thread_id
Create Date: 2026-10-03

`skills/importer.py` fetches an Agent Skill from GitHub and saves it as a library draft. Such a
version is neither an agent's nor an operator's own text, so it gets a source of its own,
`import`, and five nullable columns naming its origin: the canonical source
(`github:owner/repo/path`), the ref that was asked for, the commit it resolved to, a digest of
the skill folder's tree at that commit (a re-import with the same digest saves nothing), and the
repository's SPDX license.

Expand-only. The columns are nullable with no default, a catalog-only change on every existing
row. The source check is widened by adding the new constraint `NOT VALID` -- which takes no scan
-- and validating it separately, which holds only a `SHARE UPDATE EXCLUSIVE` lock, so saves and
publishes keep running while it checks rows that all satisfy it already.

The downgrade narrows the check back, and so refuses while any `import` version exists: dropping
those rows would delete a tenant's review history, and that is a decision for a person, not a
rollback script.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_skill_import_origin"
down_revision: str | None = "0025_plan_thread_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("origin_source", "origin_ref", "origin_commit", "origin_tree_hash", "origin_license")
_CHECK = "ck_skill_version_source"


def _replace_check(sources: str) -> None:
    op.drop_constraint(_CHECK, "skill_version", type_="check")
    op.execute(f"ALTER TABLE skill_version ADD CONSTRAINT {_CHECK} CHECK (source IN ({sources})) NOT VALID")
    op.execute(f"ALTER TABLE skill_version VALIDATE CONSTRAINT {_CHECK}")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("skill_version", sa.Column(column, sa.Text(), nullable=True))
    _replace_check("'agent', 'operator', 'import'")


def downgrade() -> None:
    imported = op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_version WHERE source = 'import'"))
    if imported:
        raise RuntimeError(
            f"{imported} skill versions were imported; archive or remove them before downgrading "
            "past 0026_skill_import_origin"
        )
    _replace_check("'agent', 'operator'")
    for column in reversed(_COLUMNS):
        op.drop_column("skill_version", column)
