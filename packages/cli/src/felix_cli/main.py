"""Felix CLI — migrate, eval, mint-jwt, bundle-manifests, version."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich import print as rprint

from felix_cli import __version__

if TYPE_CHECKING:
    from felix.config import Settings

app = typer.Typer(
    name="felix",
    help="Felix agents harness CLI.",
    no_args_is_help=True,
)


@app.callback()
def _root(ctx: typer.Context) -> None:
    """Felix agents harness CLI."""
    from felix.config import get_settings

    # One more process against the same database: name its connections. In a callback
    # rather than at import, so importing this module for a helper stamps nothing.
    #
    # Except for the subcommands that are not a CLI invocation at all but a long-lived
    # process. `stamp_process_role` is first-write-wins, so stamping "cli" here left
    # `felix temporal-worker` showing up in pg_stat_activity as felix-cli for as long as it
    # ran — indistinguishable from someone's shell, which is what the stamp exists to avoid.
    if ctx.invoked_subcommand in _LONG_RUNNING:
        return
    get_settings().stamp_process_role("cli")


# Subcommands that run a server rather than doing a thing and exiting. They stamp their own
# process role, so the root callback must not claim it first.
_LONG_RUNNING = frozenset({"temporal-worker"})


def _load_plugins() -> list[str]:
    """Discover ``felix.plugins`` entry points so plugin patterns and tools exist.

    Without this the CLI saw only built-ins: a manifest naming a plugin-registered
    pattern or tool validated as broken here while working against the API.
    Importing ``felix.patterns`` registers the built-in patterns and providers,
    which also happens at import time.
    """
    import felix.patterns  # noqa: F401 — import-time pattern registration
    from felix.plugins import get_registry, load_optional_plugins

    load_optional_plugins()
    return [str(getattr(p, "name", p)) for p in get_registry().plugins]


@app.command("version")
def version_cmd() -> None:
    """Print Felix CLI / harness version."""
    try:
        from felix import __version__ as harness_version
    except ImportError:
        harness_version = "unknown"
    rprint(f"felix-cli {__version__} (harness {harness_version})")


@app.command("migrate")
def migrate(
    revision: str = typer.Argument("head", help="Alembic revision target."),
) -> None:
    """Apply Alembic migrations."""
    from alembic import command
    from felix.config import get_settings
    from felix.db.migrations import alembic_config
    from felix.db.session import _use_memory

    # The friendly half of the refusal. `migrations/env.py:get_url` refuses it too, for the
    # three other ways into Alembic (`alembic current`, offline SQL, the conformance
    # override) — this one exists so the common path gets a message rather than a traceback.
    # `_use_memory` rather than a fourth spelling of the same predicate.
    if _use_memory(get_settings()):
        typer.echo(
            "FELIX_DATABASE_URL is memory:// — the in-memory test path has no schema to "
            "migrate. Point it at Postgres, e.g. "
            "postgresql+psycopg://felix:felix@localhost:5432/felix",
            err=True,
        )
        # 2, not 1: click's convention for "the invocation was wrong", which this is — the
        # command did not fail, it was asked to migrate something that cannot be migrated.
        raise typer.Exit(2)

    command.upgrade(alembic_config(), revision)
    rprint(f"[green]migrated to {revision}[/green]")


@app.command("eval")
def eval_cmd(
    dataset: str = typer.Option(..., "--dataset", "-d", help="Dataset name."),
    manifest: str = typer.Option(..., "--manifest", "-m", help="Candidate manifest."),
    tenant: str = typer.Option("default", "--tenant", "-t"),
    fixture: Path | None = typer.Option(
        None,
        "--fixture",
        "-f",
        help="Load dataset JSON before running (upsert).",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Score with rubric mock_answer/expect (no live model).",
    ),
    llm_judge: bool = typer.Option(
        False,
        "--llm-judge",
        help="Score with an LLM judge (ignored with --mock).",
    ),
) -> None:
    """Run an offline eval against a dataset."""
    import asyncio

    from felix.config import get_settings
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    _load_plugins()
    settings = get_settings()

    async def _run() -> None:
        name = dataset
        if fixture is not None:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            name = str(payload.get("name") or dataset)
            await eval_store.put_dataset(
                settings,
                tenant,
                name,
                description=str(payload.get("description") or ""),
                items=list(payload.get("items") or []),
            )
            # stderr: stdout is the run dict below, which CI parses. A progress line sharing
            # that stream is one more thing between a caller and the result.
            typer.echo(f"loaded fixture {fixture} → dataset={name}", err=True)
        result = await start_run(
            settings,
            tenant_id=tenant,
            dataset_name=name,
            candidate_manifest=manifest,
            mock=mock,
            use_llm_judge=llm_judge and not mock,
            deterministic_judge=not llm_judge,
        )
        # The `eval` CI job parses this dict — it asserts a pass_count of 0, the presence of
        # score rows and the absence of errors on the negative fixture. Replacing it with a
        # summary line means updating `.github/workflows/ci.yml` in the same change.
        # JSON on one line, because this output has a parser: `scripts/eval-counter-smoke.sh`
        # reads it in CI. Through rich it was pretty-printed across 38 lines with any value
        # longer than the console width split mid-token — the fixtures have short answers so
        # CI survived, a real run's would not. A Python repr fixed that but left a format only
        # Python reads, so the gate matched substrings; this one it can parse. `default=str`
        # so a field that is not serializable degrades instead of failing the run at the last
        # step, after the work is done.
        typer.echo(json.dumps(result, default=str))
        fails = int(result.get("fail_count") or 0)
        if fails:
            raise SystemExit(1)

    asyncio.run(_run())


@app.command("mint-jwt")
def mint_jwt(
    sub: str = typer.Option(..., "--sub", help="Subject claim."),
    tenant: str = typer.Option("default", "--tenant", "-t"),
    scopes: str = typer.Option(
        "audit:read,manifests:write",
        "--scopes",
        help="Comma-separated scopes.",
    ),
    ttl_seconds: int = typer.Option(3600, "--ttl"),
) -> None:
    """Mint a self-issued JWT using FELIX_JWKS_PRIVATE."""
    from felix.auth.context import assert_valid_tenant_id
    from felix.auth.jwt import mint_token
    from felix.config import get_settings

    settings = get_settings()
    # `mint_token` does not check the tenant, but `payload_to_principal` does at verification.
    # Without this the command exited 0 and printed a plausible token for `--tenant "acme:1"`
    # that every request answered with tenant_not_allowed — the same shape as the wrapping
    # bug: a successful-looking mint that nothing will accept.
    try:
        assert_valid_tenant_id(tenant)
    except ValueError as exc:
        typer.echo(f"--tenant is not usable: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not settings.jwks_private.strip():
        # `mint_token` raises here, which reached the operator as a traceback naming neither
        # the setting nor what to put in it.
        typer.echo(
            "FELIX_JWKS_PRIVATE is empty — minting a self-issued token needs an RSA private "
            "key in PEM form. Generate one, or use FELIX_AUTH_API_KEYS for local access.",
            err=True,
        )
        raise typer.Exit(2)
    token = mint_token(
        settings,
        sub=sub,
        tenant_id=tenant,
        scopes=[s.strip() for s in scopes.split(",") if s.strip()],
        ttl_seconds=ttl_seconds,
    )
    # Not rprint: rich wraps to the console width, and a 2048-bit RS256 token is about
    # 550 characters, so `TOKEN=$(felix mint-jwt …)` captured seven lines of base64 with
    # newlines through the middle and every request with it was rejected as invalid_token.
    # The token is the entire output of this command and exists to be piped.
    typer.echo(token)


@app.command("bundle-manifests")
def bundle_manifests(
    out: Path | None = typer.Option(None, "--out", "-o", help="Write JSON Schema / bundle summary here."),
) -> None:
    """Validate bundled manifests and list them as JSON on stdout.

    `--out` writes the same list plus the generated JSON Schema; stdout carries the list
    alone, because the schema is large and this stream is usually read by a human. The
    summary line goes to stderr either way, so stdout stays parseable.
    """
    from felix.manifests.loader import list_bundled, load_bundled
    from felix.manifests.schema import Manifest

    names = list_bundled()
    for name in names:
        load_bundled(name)
    # stderr: stdout is the JSON below, and `felix bundle-manifests > bundle.json` should be
    # a file a parser can read rather than a summary line with JSON stuck to it.
    typer.echo(f"validated {len(names)} manifests: {', '.join(names)}", err=True)
    schema = Manifest.model_json_schema()
    payload = {"manifests": names, "json_schema": schema}
    if out is not None:
        out.write_text(json.dumps(payload, indent=2))
        typer.echo(f"wrote {out}", err=True)
    else:
        # Plain, because this is machine-readable output and rich both wraps to the console
        # width and reads `[` as a markup tag. Today's bundle is short enough to survive
        # rendering — unlike the token in `mint-jwt`, which did not — so this keeps a hazard
        # away from output that will grow rather than fixing a live break.
        typer.echo(json.dumps({"manifests": names}, indent=2))


def _assert_outbound_hosts_resolve(manifest: Any, _settings: Any = None) -> None:
    """Resolve every manifest-supplied outbound URL, raising on a blocked address."""
    from felix.security.ssrf import assert_safe_outbound_url

    # Deliberately not inheriting `allow_insecure`. `allow_http=True` skips far more than
    # the http:// rule — internal names, internal suffixes and loopback literals all pass —
    # and `.env.example` ships FELIX_ALLOW_INSECURE=true, so on a developer machine this
    # lint would have accepted http://metadata.google.internal/ while claiming to check it.
    # This is a lint, not an enforcement point; leniency buys nothing here.
    spec = manifest.spec
    urls = [
        *(ref.url for ref in spec.mcp if ref.url),
        *(ref.url for ref in spec.peers if ref.url),
        *(ref.gateway_url for ref in spec.containers if ref.gateway_url),
    ]
    for url in urls:
        assert_safe_outbound_url(url)


@app.command("validate-manifest")
def validate_manifest_cmd(
    path: Path = typer.Argument(..., help="Path to a felix/v1 Agent YAML or JSON file."),
    environment: str = typer.Option(
        "development",
        "--environment",
        "-e",
        help="Assumed FELIX_ENVIRONMENT for governance checks.",
    ),
    resolve_egress: bool = typer.Option(
        True,
        "--resolve-egress/--no-resolve-egress",
        help="Resolve every outbound hostname and reject blocked addresses (needs DNS).",
    ),
) -> None:
    """Validate a manifest schema + opt-in governance frameworks (GitOps CI)."""
    from felix.config import Settings
    from felix.manifests.governance import GovernanceError, validate_for_write, validate_governance
    from felix.manifests.loader import load_manifest_file
    from felix.patterns.registry import list_patterns

    _load_plugins()
    settings = Settings(environment=environment)  # type: ignore[arg-type]
    try:
        manifest = load_manifest_file(path)
        validate_governance(manifest, settings)
        # The same refusals `PUT /manifests` makes, so `ok` here means the store would take it.
        validate_for_write(manifest, settings)
        # The registry is open, so this is the only place a bad pattern name can be
        # caught before build time.
        pattern = manifest.spec.pattern
        if pattern not in list_patterns():
            known = ", ".join(sorted(list_patterns()))
            raise ValueError(f"unknown pattern {pattern!r} (registered: {known})")
        # The schema validators are syntactic — resolving there meant a blocking
        # getaddrinfo on the API event loop for every ref on every read and write, and it
        # never failed closed anyway. The resolving check belongs here, where an author is
        # waiting on a CLI rather than a request, and at dial time, where it is
        # authoritative. `--no-resolve-egress` for an air-gapped CI runner.
        if resolve_egress:
            _assert_outbound_hosts_resolve(manifest)
    except GovernanceError as exc:
        rprint(f"[red]governance fail[/red] {path}: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:
        rprint(f"[red]invalid[/red] {path}: {exc}")
        raise SystemExit(1) from exc
    rprint(f"[green]ok[/green] {path} ({manifest.metadata.name})")


@dataclass(frozen=True)
class Finding:
    """One doctor row. `detail` is a value and prints either way; `remedy` prints on FAIL."""

    label: str
    passed: bool
    detail: str = ""
    remedy: str = ""


def _otel_private_or_tls(settings: Settings) -> tuple[bool, str]:
    """Whether spans leave over TLS or stay on the host, judged by the exporters' own rule."""
    from urllib.parse import urlsplit

    from felix.config import _is_loopback_host
    from felix.observability.tracing import otel_transport

    protocol, tls = otel_transport(settings)
    endpoint = settings.otel_endpoint
    # The gRPC exporter accepts a schemeless `host:port`; urlsplit needs the `//` to see a host.
    host = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}").hostname or ""
    where = f"{protocol} to {host or settings.otel_endpoint}"
    return tls or _is_loopback_host(host), f"{'tls' if tls else 'plaintext'} ({where})"


def _capability_findings(settings: Settings) -> list[Finding]:
    """Things that are configured and cannot work — true in **every** environment.

    Separate from `_posture_findings`, which returns early under
    `FELIX_ENVIRONMENT=development`. Development is what `deploy/docker/compose.yml`
    defaults to, so a row placed there would be skipped for exactly the operator it is
    for: `make up`, forget `FELIX_DOCKER_EXTRAS=otel`, run doctor, see nothing. "Is the
    exporter installed" is not a judgement about how exposed a deployment is — it is a
    fact about whether a switch that is on does anything, and that is worth saying
    wherever it is false.
    """
    if not settings.otel_enabled:
        return []
    from felix.observability.tracing import trace_exporter_available

    return [
        # The lean image and the lean install both ship without the extra, and the only
        # other signal is one warning line at startup — after which the stack looks
        # healthy in every respect except that the backend stays empty.
        #
        # "(this process)" because that is what an import probe can answer. Running doctor
        # on a lean host venv says nothing about the container, which is where export
        # actually happens — `docker compose exec api felix doctor` asks that one.
        Finding(
            "otel exporter is installed",
            trace_exporter_available(settings),
            f"FELIX_OTEL_PROTOCOL={settings.otel_protocol} (this process)",
            "FELIX_OTEL_ENABLED=true exports nothing without the otel extra for this "
            "protocol; uv sync --extra otel, or build the image with "
            "FELIX_DOCKER_EXTRAS=otel and ask the container: "
            "docker compose exec api felix doctor",
        )
    ]


def _posture_findings(settings: Settings) -> list[Finding]:
    """What doctor says about the deployment's posture.

    Each of these is legal to configure and quietly weakens the deployment, so doctor
    says so rather than leaving it to a reader of `.env` to notice. `validate_runtime`
    refuses the combinations that are never right; these are the ones that are right
    only in development, or right only with a companion setting.
    """
    from felix.auth.jwt import parse_verifiers
    from felix.config import _is_loopback_host

    rows: list[Finding] = []
    development = settings.environment == "development"
    if settings.auth_mode == "none":
        # Under `none`, allow_insecure is the acknowledgement the boot guard demands; under
        # real auth the flag has no effect outside development, so there is nothing to judge.
        rows.append(
            Finding(
                "allow_insecure (required for auth_mode=none outside development)",
                settings.allow_insecure or development,
                f"allow_insecure={settings.allow_insecure}",
                "set FELIX_ALLOW_INSECURE=true, or FELIX_AUTH_MODE=api_key|jwt",
            )
        )
        rows.append(
            Finding(
                "auth_mode=none binds loopback only",
                _is_loopback_host(settings.host),
                f"host={settings.host}",
            )
        )
    if development:
        return rows
    if settings.auth_mode == "jwt":
        # Only a verifier in `claim` mode reads a tenant claim; `fixed` and `issuer` never do.
        claim_mode = any(v.tenant_mode == "claim" for v in parse_verifiers(settings.jwt_verifiers))
        if claim_mode:
            rows.append(
                Finding(
                    "allowed_tenants pins the tenant claim",
                    bool(settings.allowed_tenants.strip()),
                    f"FELIX_ALLOWED_TENANTS={settings.allowed_tenants or '(empty)'}",
                    "any tenant a JWT claims is accepted; list the tenants, or use ;tenant=fixed:<tenant>",
                )
            )
    if settings.otel_enabled:
        tls, transport = _otel_private_or_tls(settings)
        rows.append(
            Finding(
                "otel exporter is private or TLS",
                tls,
                transport,
                "spans carry user and tenant ids; use https://, FELIX_OTEL_INSECURE=false, or a "
                "local collector",
            )
        )
        rows.append(
            Finding(
                "otel spans exclude prompts",
                not settings.otel_capture_content,
                f"FELIX_OTEL_CAPTURE_CONTENT={str(settings.otel_capture_content).lower()}",
                "prompts and completions in spans are outside content screening; turn it off "
                "outside development",
            )
        )
    return rows


@app.command("doctor")
def doctor_cmd() -> None:
    """Check runtime configuration (read-only)."""
    import asyncio
    from pathlib import Path as P

    from felix.config import get_settings

    settings = get_settings()
    ok = True

    def check(label: str, passed: bool, detail: str = "", *, remedy: str = "") -> None:
        nonlocal ok
        mark = "[green]ok[/green]" if passed else "[red]FAIL[/red]"
        if not passed:
            ok = False
        suffix = f" — {detail}" if detail else ""
        # A remedy is a sentence about the failure; on a passing row it would be a lie.
        if remedy and not passed:
            suffix += f" — {remedy}"
        rprint(f"  {mark}  {label}{suffix}")

    rprint("[bold]Felix doctor[/bold]")
    # Report what the seam actually discovered — a plugin that failed to import is
    # only a log line otherwise, so a silently-absent feature looks like a bug in core.
    from felix.patterns.registry import list_patterns

    plugin_names = _load_plugins()
    rprint(f"  [dim]plugins[/dim]  {', '.join(plugin_names) if plugin_names else 'none installed'}")
    rprint(f"  [dim]patterns[/dim] {', '.join(sorted(list_patterns()))}")
    # `_load_plugins()` above populated the authenticator registry, so a
    # plugin-registered mode is valid here. Checking the built-in set alone made
    # doctor red-FAIL the very seam an operator had just installed.
    from felix.auth.context import BUILTIN_AUTH_MODES
    from felix.plugins import get_registry

    mode = settings.auth_mode
    mode_ok = mode in BUILTIN_AUTH_MODES or get_registry().authenticator_builder(mode) is not None
    detail = mode if mode in BUILTIN_AUTH_MODES else f"{mode} (plugin)" if mode_ok else mode
    check("auth_mode", mode_ok, detail)

    # Posture, not health: reported through the same channel as `patterns` and the mcp-stdio
    # line, because a green "ok" that can never be red trains the eye to skip it.
    if settings.bundled_only:
        rprint("  [dim]manifest source[/dim] bundled — image only, write routes not mounted")
    else:
        rprint("  [dim]manifest source[/dim] store — tenant Postgres version, then bundled")

    # Every open backend setting resolved against its registry, reported rather than
    # raised — doctor's job is to list what is wrong, not to stop at the first thing.
    try:
        settings._validate_registry_backed_settings()
        check("backends resolve", True)
    except RuntimeError as exc:
        check("backends resolve", False, str(exc))
    # Two lists, because they are skipped differently: posture is a production-only
    # judgement, while "this is switched on and cannot work" is true in any environment.
    for row in _capability_findings(settings) + _posture_findings(settings):
        check(row.label, row.passed, row.detail, remedy=row.remedy)
    if settings.environment == "development":
        rprint("  [dim]posture[/dim]  production posture checks skipped — FELIX_ENVIRONMENT=development")
    from felix.security.stdio_policy import allowed_commands, describe_allowlist

    # Not a failure either way — stdio off is the safe default; on is a deliberate choice.
    rprint(
        f"  [green]ok[/green]  mcp stdio — {describe_allowlist(settings)}"
        + ("" if allowed_commands(settings) else " (safe default)")
    )
    if settings.auth_mode == "jwt":
        check("jwks_public configured", bool(settings.jwks_public.strip()))
        check("jwt_verifiers configured", bool(settings.jwt_verifiers.strip()))
    if settings.auth_mode == "api_key":
        check("auth_api_keys configured", bool(settings.auth_api_keys.strip()))
    if settings.auth_mode != "none" or settings.environment == "production":
        check(
            "consumer_shared_secret (for /internal)",
            bool(settings.consumer_shared_secret.strip()),
        )
    check(
        "object_store",
        settings.object_store in {"fs", "s3", "gcs", "memory"},
        settings.object_store,
    )
    check(
        "durability",
        settings.durability in {"fibers", "temporal"},
        settings.durability,
    )
    if settings.durability == "temporal":
        try:
            import temporalio  # noqa: F401
        except ImportError:
            check(
                "temporal extra",
                False,
                "uv sync --extra temporal",
            )
        else:
            check("temporal extra", True, settings.temporal_host)
    data = P(settings.data_dir)
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".felix-doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        check("data_dir writable", True, str(data))
    except OSError as exc:
        check("data_dir writable", False, str(exc))

    async def _ping() -> None:
        # Database
        if settings.database_url.startswith("memory://"):
            check("database", True, "memory://")
        else:
            try:
                from felix.db.session import get_engine
                from sqlalchemy import text

                engine = get_engine(settings.database_url)
                rls_on = False
                exempt = False
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                    # Is the policy live, and does this role escape it?
                    rls_on = bool(
                        await conn.scalar(
                            text(
                                "SELECT bool_or(relrowsecurity) FROM pg_class "
                                "WHERE relname = 'session_events'"
                            )
                        )
                    )
                    exempt = bool(
                        await conn.scalar(
                            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
                        )
                    )
                    statement_timeout = str(await conn.scalar(text("SHOW statement_timeout")) or "0")
                check("database", True, "reachable")
                # Reported, not judged: the only place a statement timeout can be set is the
                # server or the role — a client-side one does not survive a pooler.
                rprint(
                    f"  [dim]statement_timeout[/dim] {statement_timeout}"
                    + (
                        " (none — set it on the role: ALTER ROLE ... SET statement_timeout)"
                        if statement_timeout == "0"
                        else ""
                    )
                )

                # RLS coherence. The schema half (migration 0006) and the runtime
                # half (FELIX_DATABASE_RLS) can disagree, and both directions are
                # silent in a running system: policies without the flag means the
                # app bypasses them, so nothing is enforced; the flag without
                # policies means nothing is enforcing it either.
                if settings.database_rls and not rls_on:
                    check(
                        "tenant RLS",
                        False,
                        "FELIX_DATABASE_RLS=true but no policies — run `felix migrate head`",
                    )
                elif rls_on and not settings.database_rls:
                    # Not a failure: the supported opt-out. The query layer still
                    # scopes every read and write. Said plainly rather than left
                    # to be discovered.
                    rprint(
                        "  [green]ok[/green]  tenant RLS — policies present, "
                        "FELIX_DATABASE_RLS=false so the app bypasses them "
                        "(query-layer scoping still applies)"
                    )
                elif rls_on and settings.database_rls:
                    check(
                        "tenant RLS",
                        not exempt,
                        "enforced"
                        if not exempt
                        else "policies active but this role is superuser/BYPASSRLS, "
                        "which skips them entirely",
                    )
            except Exception as exc:
                check("database", False, str(exc)[:120])

        # "Reachable" said nothing about the schema: a deploy that skipped `felix migrate
        # head` looked healthy until the first query hit a missing column. Outside the
        # block above so a memory:// run reports it too (trivially at head).
        try:
            from felix.db.migrations import migration_state

            state = await migration_state(settings)
            check(
                "migrations at head",
                state.at_head,
                f"database={state.current or 'unmigrated'} code={state.head}",
                remedy="run `felix migrate head`",
            )
        except Exception as exc:
            check("migrations at head", False, str(exc)[:120])

        # Redis / Valkey. Not optional outside development: approvals and client-tool
        # answers cross from the API to the worker through it, and the in-process fallback
        # that takes over when it is missing or down cannot deliver them.
        redis_label = "redis (cross-process approvals, prompts, rate limits)"
        if not settings.redis_url.strip():
            if settings.environment == "development":
                check(
                    redis_label,
                    True,
                    "FELIX_REDIS_URL empty — single process only; required outside development",
                )
            else:
                check(
                    redis_label,
                    False,
                    "FELIX_REDIS_URL empty — durable runs waiting on an approval would time out",
                )
        else:
            # The same bounded probe `/ready` runs, so the two cannot disagree on "reachable".
            from felix.health import probe_redis, timed_probe

            probe = await timed_probe("redis", probe_redis(settings))
            detail = (
                probe.detail
                if probe.ok
                else f"unreachable, approvals will not cross processes: {probe.detail}"
            )
            check(redis_label, probe.ok, detail)

        # Object store factory
        try:
            from felix.storage import build_object_store

            store = build_object_store(settings)
            check("object_store backend", store is not None, type(store).__name__)
        except Exception as exc:
            check("object_store backend", False, str(exc)[:120])

        # Warehouse
        try:
            from felix.warehouse import build_warehouse

            wh = build_warehouse(settings)
            check("warehouse", True, wh.name)
        except Exception as exc:
            check("warehouse", False, str(exc)[:120])

    asyncio.run(_ping())
    raise SystemExit(0 if ok else 1)


@app.command("temporal-worker")
def temporal_worker_cmd() -> None:
    """Run a Temporal worker for durable fibers (task queue felix-fibers)."""
    import asyncio

    from felix.config import get_settings
    from felix.durability.temporal import run_worker

    settings = get_settings()
    # Named here rather than by the root callback, which skips this subcommand for exactly
    # this reason. Matches `felix_worker.main:temporal_main`, the console script that runs the
    # same worker under Compose.
    settings.stamp_process_role("temporal-worker")
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    app()
