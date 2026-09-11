"""Every `felix` subcommand, invoked.

`tests/unit/test_entrypoint_wiring.py` proves each `[project.scripts]` target resolves to a
callable. That is where the console script ends and where this file starts: nothing ran the
bodies. `doctor` and `validate-manifest` had tests of their own; `version`, `migrate`,
`mint-jwt`, `bundle-manifests` and `temporal-worker` had none.

Running them found five defects, four of them the same shape — a command that exits 0, looks
right on screen, and hands back something nothing downstream accepts:

- `mint-jwt` printed its token through a renderer that wraps at the console width, so a
  captured token carried newlines and every request with it was rejected.
- `mint-jwt` accepted a `--tenant` that the verifier refuses, and minted a token for it.
- `mint-jwt` with no signing key died on an unhandled `RuntimeError`.
- `migrate` met the in-memory URL with a raw SQLAlchemy dialect traceback.
- `temporal-worker` ran for as long as the process lived naming its Postgres connections
  `felix-cli`, because the root callback stamps first and the stamp is first-write-wins.

So the assertions here are about the contract each command has with whatever consumes it — a
shell capturing a token, a parser reading the bundle, Postgres reading a connection name, an
operator reading an error — rather than the exit code alone.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
from typing import Any

import pytest
from felix.config import Settings, get_settings
from felix_cli.main import app
from typer.testing import CliRunner

ISSUER = "felix-self"
VERIFIERS = f"self:{ISSUER}"


@pytest.fixture(autouse=True)
def _cli_environment(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin the console width, and keep the process-role stamp out of the next test.

    Rich wraps to the console width whether or not stdout is a tty, and that wrapping is what
    broke `mint-jwt` — so on a machine exporting a `COLUMNS` wider than a 536-character token,
    the test guarding that fix would pass against the unfixed code.

    Setting `COLUMNS` alone does not pin it. Rich reads the variable once, in
    `Console.__init__`, and caches it; `rich.print` uses one process-global console built on
    first use, which in a full-suite run is some earlier file's `validate-manifest`
    invocation. By the time this fixture runs the width is already frozen. So the console is
    pinned directly, and `COLUMNS` is kept for click's own formatter.

    `_root` also stamps a process role on the cached `Settings`, and these tests rewrite the
    environment under it.
    """
    import rich

    monkeypatch.setenv("COLUMNS", "80")
    # `_width`, not the public setter: `setattr(console, "width", 80)` reads the computed
    # value first, so monkeypatch's undo would freeze the console at an int for the session.
    monkeypatch.setattr(rich.get_console(), "_width", 80)
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
    """Both versions resolve, which is the complement of the fallback below.

    The CLI and the harness are separate workspace members, so this goes red if `felix`
    grows a module-scope import that a lean install cannot satisfy — the command would keep
    exiting 0 while reporting a version nobody can act on.
    """
    from felix_cli import __version__ as cli_version

    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert cli_version in result.output, result.output
    assert "unknown" not in result.output, result.output


def test_version_says_unknown_rather_than_failing_without_the_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback branch itself, which the test above is the complement of.

    A CLI installed without `felix` beside it should still answer `version` — that is the
    one command someone runs to find out what they have.
    """
    import sys

    monkeypatch.setitem(sys.modules, "felix", None)

    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output, result.output


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
    # The reason, not just the absence of a principal: `VerifyFail` has no `principal`
    # attribute at all, so a `getattr(..., None) is None` check also passes for a bad
    # signature or no matching verifier, and would keep passing if the ttl stopped being
    # applied for some other reason entirely.
    assert getattr(refused, "principal", None) is None, "an expired token verified"
    assert refused.reason == "expired", refused.reason


def test_mint_jwt_without_a_signing_key_prints_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing closed matters more here than anywhere else in the CLI.

    `mint_token` raises when `FELIX_JWKS_PRIVATE` is empty. What this pins is that the
    failure reaches the caller as a non-zero exit with nothing token-shaped on stdout — a
    script doing `TOKEN=$(felix mint-jwt …)` must not end up with a half-message in `$TOKEN`.
    """
    monkeypatch.setenv("FELIX_JWKS_PRIVATE", "")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["mint-jwt", "--sub", "ops"])

    # 2 like `migrate`, not merely non-zero: an unhandled exception also exits non-zero, and
    # that is what this used to be — a RuntimeError traceback naming neither the setting nor
    # what to put in it, which is the defect shape the `migrate` guard exists to remove.
    assert result.exit_code == 2, result.output
    assert "FELIX_JWKS_PRIVATE" in result.output, result.output

    # Nothing token-shaped anywhere in the output: three dot-separated base64url segments is
    # what a capturing script would take for a token.
    jws = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    assert jws.search(result.output) is None, result.output


def test_mint_jwt_refuses_a_tenant_the_verifier_would_reject(
    rsa_keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shape as the wrapping bug: a mint that succeeds and a token nothing accepts.

    `mint_token` puts the tenant straight into the claims, while `payload_to_principal`
    resolves it through `assert_valid_tenant_id` at verification — so a tenant carrying a
    delimiter produced a plausible token, exit 0, and `tenant_not_allowed` on every request.
    """
    private_pem, _public_pem = rsa_keypair
    monkeypatch.setenv("FELIX_JWKS_PRIVATE", private_pem)
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["mint-jwt", "--sub", "ops", "--tenant", "acme:one"])

    assert result.exit_code == 2, result.output
    assert "--tenant" in result.output, result.output
    jws = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    assert jws.search(result.output) is None, result.output


def test_bundle_manifests_writes_every_bundled_manifest(tmp_path: pathlib.Path) -> None:
    """The `--out` file is the machine path, and it is what `make schema` and CI consume."""
    from felix.manifests.loader import list_bundled

    out = tmp_path / "bundle.json"
    result = CliRunner().invoke(app, ["bundle-manifests", "--out", str(out)])

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    from felix.manifests.schema import Manifest

    assert payload["manifests"] == list(list_bundled()), payload["manifests"]
    # The schema itself, not that it has properties: one generated from the wrong model
    # satisfies a truthiness check, and `schemas/manifest.schema.json` is generated from this.
    assert payload["json_schema"] == Manifest.model_json_schema(), "the emitted schema drifted"


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
    # The whole of stdout, with no slicing: the summary line goes to stderr so that
    # `felix bundle-manifests > bundle.json` is a file a parser can read.
    assert json.loads(result.stdout)["manifests"] == list(list_bundled()), result.stdout


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


def test_migrate_upgrades_to_head_against_a_real_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, without which the guard could refuse every URL and stay green.

    `scripts/test.sh` exports `memory://`, so the refusal above is the environment's default
    answer — mutating the guard to fire unconditionally passed. Alembic is substituted here
    because the assertion is about what the command asks for, not about migrating anything.
    """
    from alembic import command as alembic_command

    # Port 1, not 5432: `command.upgrade` is substituted below, and if a refactor ever slips
    # that substitution the test should fail to connect rather than reach whatever Postgres
    # the developer has running on the port Compose publishes.
    monkeypatch.setenv("FELIX_DATABASE_URL", "postgresql+psycopg://felix:felix@127.0.0.1:1/felix")
    get_settings.cache_clear()
    upgrades: list[str] = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda _cfg, rev: upgrades.append(rev))

    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code == 0, result.output
    # "head" is the default argument, which is how every deploy invokes it.
    assert upgrades == ["head"], upgrades


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
    real_signature = inspect.signature(temporal_module.run_worker)
    calls: list[inspect.BoundArguments] = []

    async def _capture(*args: Any, **kwargs: Any) -> None:
        # Bound against the *real* signature, so a parameter added to `run_worker` and not to
        # the call site fails here instead of at a worker's first start. A fake with a fixed
        # parameter list would accept the stale call forever.
        calls.append(real_signature.bind(*args, **kwargs))

    monkeypatch.setattr(temporal_module, "run_worker", _capture)

    result = CliRunner().invoke(app, ["temporal-worker"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1, calls
    settings = calls[0].arguments["settings"]
    assert settings is get_settings(), "the worker was handed settings the process does not share"
    # The property the stamp exists for. The root callback stamps "cli" and the stamp is
    # first-write-wins, so this command used to run for days showing up in pg_stat_activity
    # as felix-cli — indistinguishable from somebody's shell.
    assert settings.process_role == "temporal-worker", settings.process_role
