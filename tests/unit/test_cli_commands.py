"""Every `felix` subcommand, invoked.

`tests/unit/test_entrypoint_wiring.py` proves each `[project.scripts]` target resolves to a
callable. That is where the console script ends and where this file starts: nothing ran the
bodies. `doctor` and `validate-manifest` had tests of their own; `version`, `migrate`,
`mint-jwt`, `bundle-manifests` and `temporal-worker` had none, and running them found three
defects that a reading would not have.

The assertions are about the contract each command has with whatever consumes it — a shell
capturing a token, a JSON parser reading a bundle, an operator reading an error — not about
the exit code alone.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
from felix.config import Settings, get_settings
from felix_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.usefixtures("_reset_process_settings")

ISSUER = "felix-self"
VERIFIERS = f"self:{ISSUER}"


@pytest.fixture(autouse=True)
def _reset_process_settings() -> Any:
    """`_root` stamps a process role on the cached `Settings`, and these tests rewrite the env.

    Clearing on the way out keeps that out of whatever runs next.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """A throwaway RS256 keypair, generated once — `mint_token` requires a real private key."""
    from joserfc import jwk

    key = jwk.RSAKey.generate_key(2048)
    return key.as_pem(private=True).decode(), key.as_pem(private=False).decode()


def _verify(token: str, public_pem: str) -> Any:
    """Check a token the way the API does, rather than decoding it a second way here."""
    from felix.auth.jwt import parse_verifiers, verify_jwt

    settings = Settings(
        database_url="memory://cli",
        object_store="memory",
        jwks_public=public_pem,
        jwt_verifiers=VERIFIERS,
    )
    return verify_jwt(token, parse_verifiers(VERIFIERS), jwks_public=public_pem, settings=settings)


def test_version_names_the_harness_it_is_installed_beside() -> None:
    """`version` falls back to "unknown" when `felix` cannot be imported.

    That fallback is the interesting branch: the CLI and the harness are separate workspace
    members, so a packaging change can leave the CLI installed without the harness, and the
    command would keep exiting 0 while reporting a version nobody can act on.
    """
    from felix_cli import __version__ as cli_version

    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert cli_version in result.output, result.output
    assert "unknown" not in result.output, result.output


def test_mint_jwt_emits_one_line_that_the_api_would_accept(
    rsa_keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token is for capturing, so being one line is part of being correct.

    This printed through rich, which wraps to the console width. A 2048-bit RS256 token is
    about 550 characters, so `TOKEN=$(felix mint-jwt …)` captured seven lines of base64 with
    newlines through the middle, and every request made with it was rejected — while the
    command exited 0 and the token looked right on screen. `deploy/GOVERNANCE.md` documents
    exactly that invocation.
    """
    private_pem, public_pem = rsa_keypair
    monkeypatch.setenv("FELIX_JWKS_PRIVATE", private_pem)
    get_settings.cache_clear()

    result = CliRunner().invoke(
        app,
        ["mint-jwt", "--sub", "ops", "--tenant", "acme", "--scopes", "chat:write,tools:calc"],
    )
    assert result.exit_code == 0, result.output

    token = result.output.strip()
    assert "\n" not in token, f"the token was wrapped; a captured copy is unusable:\n{token}"
    assert token.count(".") == 2, f"not a three-part JWS: {token}"
    assert token == "".join(token.split()), "the token carries whitespace"

    verified = _verify(token, public_pem)
    principal = getattr(verified, "principal", None)
    assert principal is not None, getattr(verified, "reason", verified)
    assert principal.subject == "ops"
    assert principal.tenant_id == "acme"
    assert principal.scopes == frozenset({"chat:write", "tools:calc"})


def test_mint_jwt_applies_the_requested_ttl(
    rsa_keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both directions, because a ttl that is read but not applied looks identical.

    The expiry is the only thing limiting a minted token, and `verify_jwt` treats `exp` as
    essential precisely because a token minted without one was once accepted forever.
    """
    private_pem, public_pem = rsa_keypair
    monkeypatch.setenv("FELIX_JWKS_PRIVATE", private_pem)
    get_settings.cache_clear()
    runner = CliRunner()

    live = runner.invoke(app, ["mint-jwt", "--sub", "ops", "--ttl", "900"])
    assert live.exit_code == 0, live.output
    ok = _verify(live.output.strip(), public_pem)
    payload = getattr(ok, "payload", None)
    assert payload is not None, getattr(ok, "reason", ok)
    assert payload["exp"] - payload["iat"] == 900, payload

    # Already expired when it was printed: the mint succeeds, the verifier refuses it.
    stale = runner.invoke(app, ["mint-jwt", "--sub", "ops", "--ttl", "-300"])
    assert stale.exit_code == 0, stale.output
    refused = _verify(stale.output.strip(), public_pem)
    assert getattr(refused, "principal", None) is None, "an expired token verified"


def test_mint_jwt_without_a_signing_key_prints_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing closed matters more here than anywhere else in the CLI.

    `mint_token` raises when `FELIX_JWKS_PRIVATE` is empty. What this pins is that the
    failure reaches the caller as a non-zero exit with nothing token-shaped on stdout — a
    script doing `TOKEN=$(felix mint-jwt …)` must not end up with a half-message in `$TOKEN`.
    """
    monkeypatch.setenv("FELIX_JWKS_PRIVATE", "")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["mint-jwt", "--sub", "ops"])

    assert result.exit_code != 0, result.output

    # Nothing token-shaped anywhere in the output: three dot-separated base64url segments is
    # what a capturing script would take for a token.
    jws = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    assert jws.search(result.output) is None, result.output


def test_bundle_manifests_writes_every_bundled_manifest(tmp_path: pathlib.Path) -> None:
    """The `--out` file is the machine path, and it is what `make schema` and CI consume."""
    from felix.manifests.loader import list_bundled

    out = tmp_path / "bundle.json"
    result = CliRunner().invoke(app, ["bundle-manifests", "--out", str(out)])

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["manifests"] == list(list_bundled()), payload["manifests"]
    assert payload["json_schema"]["properties"], "the emitted schema has no properties"


def test_bundle_manifests_prints_json_a_parser_can_read() -> None:
    """Without `--out` the JSON goes to stdout, where something may parse it.

    It is printed rather than rendered: rich wraps to the console width and reads `[` as the
    start of a markup tag, neither of which a JSON consumer survives. Today's bundle is short
    enough that it came through rich intact — I could not make it fail — so this pins the
    contract (stdout after the summary line parses, and lists every bundled manifest) rather
    than claiming to reproduce a corruption. The token above is where that hazard was real.
    """
    from felix.manifests.loader import list_bundled

    result = CliRunner().invoke(app, ["bundle-manifests"])

    assert result.exit_code == 0, result.output
    # The human summary shares this stdout; the JSON is everything from its first brace.
    body = result.output[result.output.index("{") :]
    assert json.loads(body)["manifests"] == list(list_bundled()), body


def test_migrate_refuses_the_in_memory_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """`memory://` has no schema to migrate, and it is the value `.env` ships for tests.

    It used to reach Alembic and die on `NoSuchModuleError: Can't load plugin:
    sqlalchemy.dialects:memory` under a rich traceback, which names neither the setting that
    was wrong nor what to set it to.
    """
    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://cli")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code == 2, result.output
    assert "FELIX_DATABASE_URL" in result.output, result.output
    assert "postgres" in result.output.lower(), result.output
    assert "NoSuchModuleError" not in result.output, result.output


def test_temporal_worker_runs_the_worker_with_the_process_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam between the CLI and the durability backend, which nothing else crosses.

    The body is one `asyncio.run(run_worker(get_settings()))`, and it blocks on a Temporal
    server — so the worker itself is replaced and what gets asserted is the wiring: that the
    command reaches `run_worker` at all, and hands it the process settings rather than a
    freshly constructed default.
    """
    from tests.optional_deps import require_optional

    require_optional("temporalio", "temporal")
    from felix.durability import temporal as temporal_module

    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://temporal-cli")
    get_settings.cache_clear()
    seen: list[Any] = []

    async def _capture(settings: Any) -> None:
        seen.append(settings)

    monkeypatch.setattr(temporal_module, "run_worker", _capture)

    result = CliRunner().invoke(app, ["temporal-worker"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 1, seen
    # Identity, not equality: `Settings()` reads the same environment, so a freshly built one
    # compares equal on every field while being a different object — which is exactly the
    # substitution this is here to catch, and the one that loses the `stamp_process_role`
    # the root callback applied.
    assert seen[0] is get_settings(), "the worker was handed settings the process does not share"
