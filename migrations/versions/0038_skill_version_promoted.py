"""Skill promotion: a personal version proposed to the tenant's library.

Revision ID: 0038_skill_version_promoted
Revises: 0037_thread_state_listing_index
Create Date: 2026-10-09

`POST /skill-library/~me/{name}/versions/{version}/promote` (`skills/library.py:promote`) copies a
version of the caller's personal library into the tenant's as a draft for the ordinary review
queue. Such a version is no agent's, operator's or import's, so it gets a source of its own,
`promoted`, and `promoted_from` names the personal version it was copied from (who promoted it is
the row's `author`; the personal library's owner is never stored on the tenant's row).

Expand-only. `promoted_from` is nullable and null on every existing row -- nothing was promoted
before this revision -- so adding it rewrites nothing. The source check is dropped and re-added in
this migration's transaction, as `0026` did, holding `skill_version` under an ACCESS EXCLUSIVE lock
while the new check scans its rows; one row per skill version, so the scan is short. RLS is
unchanged: the table's policy covers every column.

The downgrade narrows the check back, and so refuses while any `promoted` version exists: dropping
those rows would delete a tenant's review history, which is a person's decision. The count runs
under `app.rls_bypass`, as `0033`'s does: the table is under forced RLS, which binds its owner too,
so without the bypass a migration role that is not a superuser (any managed Postgres) counts
nothing and the guard waves the downgrade through -- the mistake `0026`'s guard makes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_skill_version_promoted"
down_revision: str | None = "0037_thread_state_listing_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_skill_version_source"


def _replace_check(sources: str) -> None:
    op.drop_constraint(_CHECK, "skill_version", type_="check")
    op.create_check_constraint(_CHECK, "skill_version", f"source IN ({sources})")


def upgrade() -> None:
    op.add_column("skill_version", sa.Column("promoted_from", sa.Text(), nullable=True))
    _replace_check("'agent', 'operator', 'import', 'promoted'")


def _promoted_rows() -> int:
    """Promoted versions across every tenant, counted with the RLS bypass on (see the docstring)."""
    bind = op.get_bind()
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    count = int(bind.scalar(sa.text("SELECT count(*) FROM skill_version WHERE source = 'promoted'")) or 0)
    # Off again at once: nothing after the count in this transaction may read across tenants.
    op.execute("SELECT set_config('app.rls_bypass', 'off', true)")
    return count


def downgrade() -> None:
    if promoted := _promoted_rows():
        raise RuntimeError(
            f"{promoted} skill versions were promoted from personal libraries; remove them from "
            "skill_version and skill_file before downgrading past 0038_skill_version_promoted"
        )
    _replace_check("'agent', 'operator', 'import'")
    op.drop_column("skill_version", "promoted_from")
