"""Personal skills: an owner in the key of the skill library's three tables.

Revision ID: 0033_skill_owner
Revises: 0032_skill_file_normalized
Create Date: 2026-10-07

The library was the tenant's alone: `skill`, `skill_version` and `skill_file` keyed on the
tenant and the skill's name, so a skill one person or their agent saved was the whole org's once
published. Each table gains `owner`, and the primary key becomes `(tenant_id, owner, name[,
version[, path]])`. `''` is the tenant's own library -- every existing row, so nothing is
backfilled -- and any other value is one principal's personal namespace
(`library_store.skill_owner`). Owner in the key is what lets a person keep a `notes` skill
beside the org's `notes`, and keeps a personal name from reserving that name tenant-wide, which
would tell everyone else it exists. It sits second so the key's index serves a listing of one
owner's skills.

The tables that name a skill without its versions (`skill_feedback`, `skill_eval`,
`skill_upstream`) are unchanged: feedback, evaluation and import stay org-only, and the personal
routes refuse them until that changes.

`owner` is NOT NULL with a constant default, a catalog-only change. Re-keying is not: each
primary key index is rebuilt under ACCESS EXCLUSIVE, which holds saves and catalog loads for the
length of the build. The tables hold one row per skill, version and file, so that is short. RLS
is unchanged: the policy is on `tenant_id`, and owner filtering is the store's.

The downgrade refuses while any table holds a personal row: two owners' skills of one name cannot
share the old key, a personal skill that did fit would silently become the org's, and dropping a
person's skills is a decision for a person, not a rollback script. The count runs under the RLS
bypass, as `0029`'s backfill does. Even a clean downgrade leaves personal files in the object store
(`skill-library/{tenant}/~…/`), which nothing reads afterwards.

Code from before this revision upserts `skill` on `(tenant_id, name)`, which is no longer a unique
key, so its skill saves fail until every replica runs the new code. Roll quickly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_skill_owner"
down_revision: str | None = "0032_skill_file_normalized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Each table's key without `owner`, in order.
_KEYS: dict[str, tuple[str, ...]] = {
    "skill": ("tenant_id", "name"),
    "skill_version": ("tenant_id", "name", "version"),
    "skill_file": ("tenant_id", "name", "version", "path"),
}


def _rekey(table: str, columns: Sequence[str]) -> None:
    # Postgres's own name for a key declared inline, as `0022` declared these.
    op.drop_constraint(f"{table}_pkey", table, type_="primary")
    op.create_primary_key(f"{table}_pkey", table, list(columns))


def upgrade() -> None:
    for table, (tenant, *rest) in _KEYS.items():
        op.add_column(table, sa.Column("owner", sa.Text(), nullable=False, server_default=""))
        _rekey(table, (tenant, "owner", *rest))


def _personal_rows() -> dict[str, int]:
    """Each table's personal rows, across every tenant. The tables are under forced RLS, which
    binds their owner too, so without the bypass a migration role that is not a superuser (any
    managed Postgres) counts nothing and the guard waves the downgrade through (`0029` reads
    `skill_version` the same way)."""
    bind = op.get_bind()
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    counts = {t: int(bind.scalar(sa.text(f"SELECT count(*) FROM {t} WHERE owner <> ''")) or 0) for t in _KEYS}
    # Off again at once: nothing after the count in this transaction may read across tenants.
    op.execute("SELECT set_config('app.rls_bypass', 'off', true)")
    return {t: n for t, n in counts.items() if n}


def downgrade() -> None:
    # Every table, not just versions: a personal `skill` row left behind would re-key into an
    # org skill, live pointer and all, whose versions are gone.
    if personal := _personal_rows():
        held = ", ".join(f"{n} in {t}" for t, n in personal.items())
        raise RuntimeError(
            f"personal libraries still hold rows ({held}); remove them from skill, skill_version and "
            "skill_file before downgrading past 0033_skill_owner"
        )
    for table, columns in reversed(_KEYS.items()):
        _rekey(table, columns)
        op.drop_column(table, "owner")
