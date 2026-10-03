"""`FELIX_DURABILITY=temporal` fails at startup and says what to do, rather than being ignored.

`Settings` ignores unknown keys, so dropping the field would have let a deployment keep setting
it — and keep running a Temporal server and worker nothing used — with no sign anything changed.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from pydantic import ValidationError


def _settings(**kw: object) -> Settings:
    return Settings(database_url="memory://durability", object_store="memory", **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["temporal", "Temporal", " temporal "])
def test_the_removed_backend_is_refused_with_the_way_forward(value: str) -> None:
    with pytest.raises(ValidationError) as info:
        _settings(durability=value)
    message = str(info.value)
    assert "FELIX_DURABILITY=temporal was removed" in message
    assert "Unset FELIX_DURABILITY" in message and "felix-temporal-worker" in message


def test_fibers_is_the_default_and_still_accepted() -> None:
    assert _settings().durability == "fibers"
    assert _settings(durability="fibers").durability == "fibers"


def test_the_environment_variable_reaches_the_same_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FELIX_DURABILITY", "temporal")
    with pytest.raises(ValidationError, match="was removed"):
        _settings()
