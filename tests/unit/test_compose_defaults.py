"""Compose files, checked where nothing else checks them.

A default written twice is a default that drifts.

Compose has to name a value for any setting it passes through — `${VAR:-}` sends an
empty string, which a float field rejects at startup — so the numeric knobs it exposes
repeat defaults that already live in `Settings`. Repeating them is fine; repeating them
where nothing checks is not, and the failure is quiet: the app starts, and behaves
differently under Compose than it documents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from felix.config import Settings

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "compose.yml"

# Compose variable -> the Settings field it stands in for. Every entry is a default
# written twice; this map is what stops the second copy drifting from the first.
PASSED_THROUGH = {
    "FELIX_STREAM_RESUME_IDLE_SECONDS": "stream_resume_idle_seconds",
    "FELIX_STREAM_RESUME_POLL_SECONDS": "stream_resume_poll_seconds",
    "FELIX_STREAM_RESUME_POLL_MAX_SECONDS": "stream_resume_poll_max_seconds",
    "FELIX_OTEL_ENABLED": "otel_enabled",
    "FELIX_OTEL_ENDPOINT": "otel_endpoint",
    "FELIX_OTEL_PROTOCOL": "otel_protocol",
    "FELIX_OTEL_SERVICE_NAME": "otel_service_name",
    "FELIX_OTEL_INSECURE": "otel_insecure",
    "FELIX_OTEL_SAMPLE_RATIO": "otel_sample_ratio",
    "FELIX_OTEL_CAPTURE_CONTENT": "otel_capture_content",
    "FELIX_OTEL_CAPTURE_IDENTITY": "otel_capture_identity",
    "FELIX_OTEL_LOGS": "otel_logs",
}

# A tracing backend is something Felix sends to, so pointing at one must not require
# running it inside this project. These reach every Felix process through `x-felix-env`;
# an overlay that bundles a backend is then a convenience, never the only route.
# `FELIX_OTEL_HEADERS` carries a credential and so has no default to compare — it is the
# one member of this list absent from PASSED_THROUGH.
OTEL_PASSTHROUGH = ("FELIX_OTEL_HEADERS", *sorted(k for k in PASSED_THROUGH if "OTEL" in k))


@pytest.mark.parametrize(("env_var", "field"), sorted(PASSED_THROUGH.items()))
def test_the_compose_default_matches_the_settings_default(env_var: str, field: str) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"\$\{{{env_var}:-([^}}]*)\}}", text)
    assert match, f"{env_var} is not passed through deploy/docker/compose.yml"

    written = match.group(1)
    assert written != "", f"{env_var} defaults to an empty string; a numeric field rejects that at startup"

    expected = getattr(Settings(database_url="memory://x"), field)
    # Compared as text after normalising the two shapes Compose and pydantic spell
    # differently: a bool is `true`/`false` in YAML and `True`/`False` in Python, and a
    # float may be written `1.0` or `1`. Not `float()` on everything, which is how eight
    # of these could not be enrolled here at all — and `otel_capture_identity` is the one
    # that matters: flip it to False on privacy grounds and every Compose deployment keeps
    # exporting identity, because compose pins `true` and nothing compares the two.
    assert _same_default(written, expected), (
        f"compose defaults {env_var} to {written!r}, but Settings.{field} is {expected!r}"
    )


def _same_default(written: str, expected: object) -> bool:
    if isinstance(expected, bool):
        return written.lower() == str(expected).lower()
    if isinstance(expected, (int, float)):
        try:
            return float(written) == float(expected)
        except ValueError:
            return False
    return written == str(expected)


def test_every_overlay_is_validated_by_ci() -> None:
    """An overlay nothing parses is an overlay that rots.

    `check-yaml` cannot construct Compose's `!override` / `!reset` tags, so the overlays
    using them are excluded from it — and the CI docker job ran `compose config` on the
    base file alone, so those exclusions were trading one check for none. The overlays
    were validated by nothing at all, which is how a typo survives until someone runs
    the matching `make up-*` target.
    """
    root = COMPOSE.parent
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    overlays = sorted(p.name for p in root.glob("compose.*.yml"))
    assert overlays, "no overlays found — has deploy/docker been restructured?"

    missing = [name for name in overlays if name not in workflow]
    assert not missing, (
        f"overlays no CI step validates: {missing}. Add them to the docker job's "
        "`Compose config` step, or they are checked by nothing."
    )


@pytest.mark.parametrize("service", ["api", "worker"])
@pytest.mark.parametrize("env_var", OTEL_PASSTHROUGH)
def test_the_base_stack_can_be_pointed_at_an_external_otlp_backend(env_var: str, service: str) -> None:
    """Without this, `make up` can export to nothing at all.

    `x-felix-env` carried no FELIX_OTEL_* key and there is no `env_file`, so the only way
    to get a span out of the Compose stack was to run an overlay that stood a backend up
    inside this project — which is how the repo ended up hosting a vendor's API, worker
    and console on Felix's own Postgres, Valkey and MinIO.

    Both services, because half a deployment's spans is worse than none: the worker owns
    every periodic job (fiber resume, consolidation, retention), so a trace that ends at
    the API's 202 describes none of the work that actually ran.
    """
    from tests.compose_yaml import load_compose

    env = load_compose(COMPOSE)["services"][service]["environment"]
    assert env_var in env, (
        f"{env_var} does not reach {service}; an operator cannot point the base stack at "
        "an OTLP backend they already run"
    )
    assert f"${{{env_var}" in str(env[env_var]), (
        f"{service} pins {env_var} to a literal instead of taking it from the environment"
    )
