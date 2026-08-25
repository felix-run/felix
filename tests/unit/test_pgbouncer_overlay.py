"""The PgBouncer overlay says what it means, and keeps saying it.

An overlay is the kind of file that rots quietly: nothing imports it, no test exercises
it, and a rename in the base compose file leaves it pointing at a service that no longer
exists. It is also the file where a mistake is expensive — routing the app at a pooler
while leaving prepared statements on fails on the *sixth* query, which is late enough to
look like something else.

These are structural rather than behavioural: bringing the stack up needs Docker and
would fight a developer's running containers. The Felix-through-PgBouncer behaviour is
verified in felix-run/felix#91 against a real pooler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy" / "docker" / "compose.pgbouncer.yml"
BASE = ROOT / "deploy" / "docker" / "compose.yml"


def _compose_default(spec: object) -> str:
    """The value compose would use with nothing set in the environment.

    `${VAR:-200}` is compose interpolation, not YAML, so the parsed value is the raw
    string. Substring-matching it is how the first version of this file decided that
    `200` meant zero, because `"200}"` contains `"0}"`.
    """
    text = str(spec)
    if ":-" in text:
        return text.split(":-", 1)[1].rstrip("}")
    return text.strip("${}")


def _load(path: Path) -> dict:
    """Parse a compose file.

    `ruamel.yaml` rather than `pyyaml`: it is what the harness declares and uses, so it
    is present by contract. `yaml` happens to be importable here only as somebody
    else's transitive dependency, which is a thing that stops being true without
    warning.

    `${VAR:?err}` interpolation is compose's, not YAML's, so the raw parse leaves those
    as strings — which is all these assertions need.
    """
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}


def test_the_overlay_parses() -> None:
    assert _load(OVERLAY).get("services"), "the overlay defines no services"


def test_every_service_it_overrides_exists_in_the_base() -> None:
    """A rename in the base file would otherwise leave this silently defining a *new*
    service that nothing depends on, and the app would keep talking to Postgres direct."""
    base = set(_load(BASE).get("services") or {})
    overlay = set(_load(OVERLAY).get("services") or {})
    unknown = sorted(overlay - base - {"pgbouncer"})
    assert unknown == [], f"overlay overrides services the base does not define: {unknown}"


@pytest.mark.parametrize("service", ["api", "worker", "scheduler"])
def test_every_felix_process_is_pointed_at_the_pooler(service: str) -> None:
    """All three hold their own pool. Pointing only the API at PgBouncer would leave the
    worker and scheduler consuming the connections the pooler exists to conserve."""
    svc = (_load(OVERLAY)["services"]).get(service) or {}
    url = (svc.get("environment") or {}).get("FELIX_DATABASE_URL", "")
    assert "@pgbouncer:" in url, f"{service} still connects to {url or 'the base URL'}"


def test_the_prepared_statement_setting_agrees_with_the_pooler() -> None:
    """The one combination that fails, and fails late.

    psycopg3 auto-prepares after five executions. If PgBouncer is not tracking prepared
    statements itself, the sixth query lands on a different server connection and dies.
    Either the pooler tracks them or the client stops making them — never neither.
    """
    services = _load(OVERLAY)["services"]
    tracked = _compose_default(services["pgbouncer"]["environment"]["MAX_PREPARED_STATEMENTS"])
    prepares = _compose_default((services["api"].get("environment") or {})["FELIX_DB_PREPARED_STATEMENTS"])

    pooler_tracks = int(tracked) > 0
    client_prepares = prepares.lower() in {"true", "1", "yes"}
    assert pooler_tracks or not client_prepares, (
        f"MAX_PREPARED_STATEMENTS={tracked} with FELIX_DB_PREPARED_STATEMENTS={prepares}: "
        "the sixth query on every connection would fail"
    )


def test_the_pool_mode_is_transaction() -> None:
    """Session mode would multiplex nothing — it holds a server connection for the life
    of the client one, which is the problem rather than the fix."""
    env = _load(OVERLAY)["services"]["pgbouncer"]["environment"]
    assert env["POOL_MODE"] == "transaction"


def test_the_image_is_pinned() -> None:
    """`:latest` on the component that sits between the app and its database is how a
    deployment changes behaviour without anyone changing anything."""
    image = _load(OVERLAY)["services"]["pgbouncer"]["image"]
    assert ":" in image and not image.endswith(":latest"), image


def test_the_client_pool_is_larger_than_the_server_pool() -> None:
    """The whole point: many cheap client connections onto few server ones. Reversed,
    the pooler is a bottleneck rather than a multiplexer."""
    env = _load(OVERLAY)["services"]["pgbouncer"]["environment"]

    assert int(_compose_default(env["MAX_CLIENT_CONN"])) > int(_compose_default(env["DEFAULT_POOL_SIZE"]))


def test_make_up_pooled_uses_the_overlay() -> None:
    """A target that forgets the `-f` runs the plain stack and looks like it worked."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "compose.pgbouncer.yml" in makefile, "no make target references the overlay"
    assert "up-pooled:" in makefile
