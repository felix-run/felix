"""A secrets backend that records every name it is asked for, registered under a test-only name.

A test of a guard in front of the backend asserts what the backend *saw*: a refusal raised after
`get` ran has already fetched the value, so "it raised" alone does not prove the secret stayed put.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

BACKEND = "recording-test"


@dataclass
class RecordingSecrets:
    values: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get(self, name: str) -> str | None:
        self.calls.append(name)
        return self.values.get(name)


def install_recording_secrets(monkeypatch: pytest.MonkeyPatch, **values: str) -> RecordingSecrets:
    """Register one `RecordingSecrets` as `FELIX_SECRETS_BACKEND=recording-test` for this test."""
    from felix import secrets

    backend = RecordingSecrets(values=dict(values))

    def factory(settings: Any) -> RecordingSecrets:
        _ = settings
        return backend

    monkeypatch.setitem(secrets._backends, BACKEND, factory)
    return backend
