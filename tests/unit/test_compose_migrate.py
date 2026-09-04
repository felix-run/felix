"""The Compose stack migrates before it serves.

The schema used to be a separate `make migrate` from the host, which `up-lite` and `up-gcp`
cannot even reach (they publish no database port), and a first `make up` served a
schemaless database — `/ready` runs `SELECT 1`, so the stack reported Ready as well. The
Helm chart had a pre-install migrate Job all along; Compose had nothing.

Structural, like the pgbouncer overlay's tests: bringing the stack up needs Docker and
would fight a developer's running containers. CI runs `compose config` over every overlay,
which catches syntax; these pin the wiring — which service waits on which, and that the
overlay that pulls a published image pulls it for the migration too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "docker"
# (file, service) for every Felix process that opens the database.
FELIX_SERVICES = [
    ("compose.yml", "api"),
    ("compose.yml", "worker"),
    ("compose.yml", "scheduler"),
    ("compose.temporal.yml", "temporal-worker"),
]


def _services(name: str) -> dict:
    return load_compose(ROOT / name)["services"]


def test_migrate_is_a_one_shot_that_only_upgrades() -> None:
    migrate = _services("compose.yml")["migrate"]
    command = migrate["command"]
    assert (command if isinstance(command, list) else command.split()) == ["felix", "migrate", "head"]
    assert migrate["restart"] == "no", "a completed migration must not be restarted"
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    # Reaches Postgres directly, not the pooler: the base URL names `postgres`, and the
    # pgbouncer overlay must not redirect it (asserted below).
    assert "@postgres:5432/" in migrate["environment"]["FELIX_DATABASE_URL"]


@pytest.mark.parametrize(("file", "service"), FELIX_SERVICES)
def test_every_felix_process_waits_for_the_migration(file: str, service: str) -> None:
    """`service_started` would not do: it lets the app boot while the schema is still
    being applied, which is the window `docs/UPGRADING.md` warns about. Includes the
    Temporal overlay's worker, which opens the same database from a second file."""
    deps = _services(file)[service]["depends_on"]
    assert deps.get("migrate", {}).get("condition") == "service_completed_successfully", (
        f"{file}: {service} does not wait for migrate to complete"
    )


def test_the_published_image_overlay_migrates_from_the_same_image() -> None:
    """The gcp overlay replaces `build:` with a pulled release for every service; the
    migration has to come from that release too, or the schema applied is whatever the
    host happened to compile."""
    gcp = _services("compose.gcp.yml")
    migrate = gcp["migrate"]
    assert migrate["image"] == gcp["api"]["image"]
    assert migrate.get("pull_policy") == "always"
    # `build: !reset null` has to be written on the service itself: through a `<<:`
    # merge key Compose does not reset it, the base `build:` survives, and this parse
    # cannot tell the two apart. So the raw text is checked, and CI checks the render.
    text = (ROOT / "compose.gcp.yml").read_text(encoding="utf-8")
    for service in ("migrate", "api", "worker", "scheduler"):
        assert re.search(rf"^  {service}:\n(?:    .*\n|\n)*?    build: !reset null$", text, re.M), (
            f"{service} in compose.gcp.yml must carry an inline `build: !reset null`"
        )


def test_the_pooler_overlay_leaves_the_migration_on_postgres() -> None:
    """Migrations are a one-off admin action, not request traffic to multiplex — and a
    DDL transaction through a transaction-mode pooler is the case its own README says
    to avoid."""
    # That the app services *are* redirected is test_pgbouncer_overlay's assertion.
    assert "migrate" not in _services("compose.pgbouncer.yml"), (
        "the pgbouncer overlay must not redirect migrate through the pooler"
    )
