"""Where the schema is, versus where the code expects it to be.

`felix doctor` said "database reachable" and nothing about whether the migrations had
been applied, so a deploy that skipped `felix migrate head` looked healthy until the
first query hit a missing column. The two numbers that answer the question — the
newest revision in `migrations/versions/` and the revision stamped in the database —
are both a few lines of Alembic, kept here so doctor and any future check read them
the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config

    from felix.config import Settings

# The repo root holds alembic.ini; this file is packages/harness/src/felix/db/migrations.py.
# The workspace is installed editable (the image too), so this resolves to the checkout.
_ROOT = Path(__file__).resolve().parents[5]


def alembic_config(url: str | None = None) -> Config:
    """The project's Alembic config, optionally pointed at a URL other than the settings'.

    `script_location` in `alembic.ini` is relative to the working directory, which made
    `felix migrate` — and would have made `felix doctor` — fail from any directory but
    the repo root. It is set absolute here.
    """
    from alembic.config import Config

    ini = _ROOT / "alembic.ini"
    if not ini.is_file():
        raise FileNotFoundError(
            f"{ini} not found: migrations need the felix checkout (an editable install), not a bare wheel"
        )
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    # `prepend_sys_path = .` in the ini put the working directory ahead of site-packages
    # for every import doctor makes after this; nothing in env.py needs it.
    cfg.set_main_option("prepend_sys_path", "")
    if url:
        cfg.attributes["felix_url"] = url
    return cfg


def script_head() -> str | None:
    """The newest revision the code ships."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


@dataclass(frozen=True)
class MigrationState:
    current: str | None  # what the database is stamped with; None = never migrated
    head: str | None  # what the code ships

    @property
    def at_head(self) -> bool:
        return self.head is not None and self.current == self.head


async def migration_state(settings: Settings) -> MigrationState:
    """Compare the database's stamped revision with the code's head.

    Uses the same engine the runtime uses, so it answers for the database the process
    would actually talk to. On `memory://` there is no schema to be behind.
    """
    from alembic.runtime.migration import MigrationContext

    from felix.db.session import _use_memory, get_engine

    head = script_head()
    if _use_memory(settings):
        return MigrationState(current=head, head=head)
    engine = get_engine(settings.database_url)
    async with engine.connect() as conn:
        current = await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
    return MigrationState(current=current, head=head)


__all__ = ["MigrationState", "alembic_config", "migration_state", "script_head"]
