"""`scripts/check-compose-render.py` refuses a workspace that is a host directory.

The compose default used to bind-mount `./workspace` — inside the deployment's own checkout — at
`/workspace`, and the published image could not write it (`Errno 13` on the reference
deployment). CI renders every overlay and pipes it through the script, which needs Docker; this
drives the rule directly with a minimal rendered config, so the rule is pinned without it.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-compose-render.py"


def _check(monkeypatch: pytest.MonkeyPatch, workspace_mount: dict[str, Any]) -> tuple[int, str]:
    spec = importlib.util.spec_from_file_location("check_compose_render", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    service = {
        "image": "felix:latest",
        "depends_on": {"migrate": {"condition": "service_completed_successfully"}},
        "volumes": [{"type": "volume", "source": "felix-data", "target": "/data"}, workspace_mount],
    }
    rendered = {"services": {"api": service, "worker": dict(service)}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(rendered)))
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    return mod.main([]), err.getvalue()


def test_a_bind_mounted_workspace_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FELIX_WORKSPACE_HOST", raising=False)
    code, err = _check(
        monkeypatch, {"type": "bind", "source": "/opt/felix/workspace", "target": "/workspace"}
    )
    assert code == 1
    assert "api: /workspace is a bind mount of '/opt/felix/workspace'" in err
    assert "worker:" in err


def test_the_named_volume_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FELIX_WORKSPACE_HOST", raising=False)
    code, err = _check(monkeypatch, {"type": "volume", "source": "felix-workspace", "target": "/workspace"})
    assert code == 0, err


def test_an_operator_override_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FELIX_WORKSPACE_HOST` is the operator asking for a host path, which stays their call."""
    monkeypatch.setenv("FELIX_WORKSPACE_HOST", "/srv/felix/workspace")
    code, err = _check(
        monkeypatch, {"type": "bind", "source": "/srv/felix/workspace", "target": "/workspace"}
    )
    assert code == 0, err
