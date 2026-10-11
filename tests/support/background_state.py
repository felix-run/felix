"""Background shell commands' state, kept in a test's own directory.

`felix.tools.shell_background` keeps its state under the system temp directory, one directory per
workspace root. A test's roots are fresh `tmp_path`s, so without this each run would leave its
state behind for good, and a later run reusing a root would see an earlier one's commands.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def isolate_background_state(monkeypatch: pytest.MonkeyPatch, factory: pytest.TempPathFactory) -> Path:
    """Point the module's temp directory at a fresh one of the test's own -- never inside a
    `tmp_path` a test uses as its workspace -- and return it. A supervisor is told its entry's
    path, so nothing it does reads the temp directory again."""
    state = factory.mktemp("background-state")
    state.chmod(0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(state))
    return state
