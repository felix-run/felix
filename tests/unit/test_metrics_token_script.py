"""`scripts/metrics-token.sh`, run against a scratch `.env`: the scrape key it writes.

The key must be one `/metrics` accepts (`metrics:read`) and nothing else accepts, in a tenant of
its own -- in `default` it could drive that tenant's manifests. It upgrades only the entry it
minted before the scope gate existed, never someone else's, and refuses a pre-set token it did not
mint, since adopting one could hand any key -- the operator's -- to Prometheus's token file.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "metrics-token.sh"
OPERATOR = {"op-x": {"tenant_id": "default", "sub": "local-dev", "scopes": ["admin"]}}
WANT = {"tenant_id": "metrics", "sub": "metrics-scrape", "scopes": ["metrics:read"]}
# Prefixed like a minted token, and obviously not one.
PRESET = "sk-felix-scrape-test"


def _repo(tmp_path: Path, keys: dict[str, object], *extra: str) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    lines = [f"FELIX_AUTH_API_KEYS={json.dumps(keys, separators=(',', ':'))}", *extra]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None or shutil.which("python3") is None:
        pytest.skip("needs bash and python3")
    return subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name)], capture_output=True, text=True, check=False
    )


def _keys(repo: Path) -> dict[str, object]:
    found = re.search(r"^FELIX_AUTH_API_KEYS=(.*)$", (repo / ".env").read_text(), re.M)
    assert found
    return json.loads(found.group(1))


def test_a_fresh_key_reads_metrics_in_a_tenant_of_its_own(tmp_path: Path) -> None:
    repo = _repo(tmp_path, OPERATOR)
    assert _run(repo).returncode == 0
    token = (repo / "deploy" / "docker" / ".metrics-token").read_text()
    assert re.fullmatch(r"sk-felix-scrape-[0-9a-f]{48}", token)
    keys = _keys(repo)
    assert keys == {**OPERATOR, token: WANT}
    assert f"FELIX_METRICS_SCRAPE_TOKEN={token}" in (repo / ".env").read_text()
    # Idempotent: a second run changes nothing.
    assert _run(repo).returncode == 0
    assert _keys(repo) == keys
    assert (repo / ".env").read_text().count("FELIX_METRICS_SCRAPE_TOKEN=") == 1


def test_a_key_minted_before_the_scope_gate_is_upgraded(tmp_path: Path) -> None:
    old = {"tenant_id": "default", "sub": "metrics-scrape", "scopes": []}
    repo = _repo(tmp_path, {**OPERATOR, PRESET: old}, f'FELIX_METRICS_SCRAPE_TOKEN="{PRESET}"')
    assert _run(repo).returncode == 0
    assert _keys(repo) == {**OPERATOR, PRESET: WANT}
    # The surrounding quotes are not part of the token.
    assert (repo / "deploy" / "docker" / ".metrics-token").read_text() == PRESET


def test_an_entry_it_did_not_write_is_left_alone(tmp_path: Path) -> None:
    theirs = {"tenant_id": "default", "sub": "someone", "scopes": ["admin"]}
    repo = _repo(tmp_path, {**OPERATOR, PRESET: theirs}, f"FELIX_METRICS_SCRAPE_TOKEN={PRESET}")
    out = _run(repo)
    assert out.returncode == 0
    assert "left as it is" in out.stderr
    assert _keys(repo) == {**OPERATOR, PRESET: theirs}


def test_a_preset_token_it_did_not_mint_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, OPERATOR, "FELIX_METRICS_SCRAPE_TOKEN=op-x")
    out = _run(repo)
    assert out.returncode != 0
    assert "does not start with sk-felix-scrape-" in out.stderr
    assert _keys(repo) == OPERATOR
    assert not (repo / "deploy" / "docker" / ".metrics-token").exists()
