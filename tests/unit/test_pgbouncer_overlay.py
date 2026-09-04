"""The PgBouncer overlay says what it means, and keeps saying it.

An overlay is the kind of file that rots quietly: nothing imports it, no test exercises
it, and a rename in the base compose file leaves it pointing at a service that no longer
exists. It is also the file where a mistake is expensive — routing the app at a pooler
while leaving prepared statements on fails on the *sixth* query, which is late enough to
look like something else.

These are structural rather than behavioural: bringing the stack up needs Docker and
would fight a developer's running containers. The stack has since been booted by hand on
isolated ports -- 40 consecutive requests through the pooler, well past psycopg's
five-execution prepare threshold, against two server backends shared by api, worker, and
scheduler. That run is also what proved a structural check can pass for an image tag that
does not exist, which `test_the_image_is_pinned_to_a_tag_that_could_exist` now covers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.compose_yaml import load_compose as _load

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


def test_the_image_is_pinned_to_a_tag_that_could_exist() -> None:
    """`:latest` on the component between the app and its database is how a deployment
    changes behaviour without anyone changing anything.

    The tag shape is checked too, because "pinned and not :latest" passed happily for
    `1.25.2` -- a tag this repository invented by reading the version PgBouncer prints in
    its own startup log. edoburu publishes `vMAJOR.MINOR.PATCH-pN`, so the pull failed at
    `compose up` with a tag that had looked pinned in review and in CI. Nothing offline
    can prove a tag exists; a shape check is what catches the plausible-looking wrong one.
    """
    image = _load(OVERLAY)["services"]["pgbouncer"]["image"]
    repo, _, tag = image.partition(":")
    assert tag and tag != "latest", image
    if repo == "edoburu/pgbouncer":
        assert re.fullmatch(r"v\d+\.\d+\.\d+-p\d+", tag), (
            f"{tag!r} is not edoburu's tag scheme (vMAJOR.MINOR.PATCH-pN); "
            "a tag that does not exist fails at `compose up`, not in review"
        )


def test_the_client_pool_is_larger_than_the_server_pool() -> None:
    """The whole point: many cheap client connections onto few server ones. Reversed,
    the pooler is a bottleneck rather than a multiplexer."""
    env = _load(OVERLAY)["services"]["pgbouncer"]["environment"]

    assert int(_compose_default(env["MAX_CLIENT_CONN"])) > int(_compose_default(env["DEFAULT_POOL_SIZE"]))


def test_make_up_pooled_uses_the_overlay() -> None:
    """A target that forgets the `-f` runs the plain stack and looks like it worked.

    Resolved rather than substring-matched. The previous version asked only whether
    `compose.pgbouncer.yml` appeared *somewhere* in the Makefile -- and it appears in the
    `COMPOSE_PGB :=` definition, never in the recipe. Swapping the recipe's `$(COMPOSE_PGB)`
    for `$(COMPOSE)`, which is exactly the mistake the docstring describes, left both
    assertions green.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    variables = dict(re.findall(r"^(\w+)\s*:=\s*(.*)$", makefile, re.MULTILINE))
    recipe = re.search(r"^up-pooled:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert recipe, "no `up-pooled:` target in the Makefile"

    command = recipe.group(1)
    for _ in range(5):  # variables reference variables; COMPOSE_PGB expands to COMPOSE
        expanded = re.sub(r"\$\((\w+)\)", lambda m: variables.get(m.group(1), m.group(0)), command)
        if expanded == command:
            break
        command = expanded

    assert "compose.pgbouncer.yml" in command, f"`up-pooled` runs without the overlay: {command.strip()}"
