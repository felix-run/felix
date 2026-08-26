"""A default written twice is a default that drifts.

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

# Compose variable -> the Settings field it stands in for.
PASSED_THROUGH = {
    "FELIX_STREAM_RESUME_IDLE_SECONDS": "stream_resume_idle_seconds",
    "FELIX_STREAM_RESUME_POLL_SECONDS": "stream_resume_poll_seconds",
    "FELIX_STREAM_RESUME_POLL_MAX_SECONDS": "stream_resume_poll_max_seconds",
}


@pytest.mark.parametrize(("env_var", "field"), sorted(PASSED_THROUGH.items()))
def test_the_compose_default_matches_the_settings_default(env_var: str, field: str) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"\$\{{{env_var}:-([^}}]*)\}}", text)
    assert match, f"{env_var} is not passed through deploy/docker/compose.yml"

    written = match.group(1)
    assert written != "", f"{env_var} defaults to an empty string; a numeric field rejects that at startup"

    expected = getattr(Settings(database_url="memory://x"), field)
    assert float(written) == float(expected), (
        f"compose defaults {env_var} to {written}, but Settings.{field} is {expected}"
    )
