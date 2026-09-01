"""Felix CLI — migrate, eval, mint-jwt, bundle-manifests, version."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print as rprint

from felix_cli import __version__

app = typer.Typer(
    name="felix",
    help="Felix agents harness CLI.",
    no_args_is_help=True,
)


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
    from alembic.config import Config

    root = Path(__file__).resolve().parents[4]
    cfg = Config(str(root / "alembic.ini"))
    command.upgrade(cfg, revision)
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
            rprint(f"[green]loaded fixture[/green] {fixture} → dataset={name}")
        result = await start_run(
            settings,
            tenant_id=tenant,
            dataset_name=name,
            candidate_manifest=manifest,
            mock=mock,
            use_llm_judge=llm_judge and not mock,
            deterministic_judge=not llm_judge,
        )
        rprint(result)
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
    from felix.auth.jwt import mint_token
    from felix.config import get_settings

    settings = get_settings()
    token = mint_token(
        settings,
        sub=sub,
        tenant_id=tenant,
        scopes=[s.strip() for s in scopes.split(",") if s.strip()],
        ttl_seconds=ttl_seconds,
    )
    rprint(token)


@app.command("bundle-manifests")
def bundle_manifests(
    out: Path | None = typer.Option(None, "--out", "-o", help="Write JSON Schema / bundle summary here."),
) -> None:
    """Validate bundled manifests and optionally emit JSON Schema."""
    from felix.manifests.loader import list_bundled, load_bundled
    from felix.manifests.schema import Manifest

    names = list_bundled()
    for name in names:
        load_bundled(name)
    rprint(f"[green]validated {len(names)} manifests:[/green] {', '.join(names)}")
    schema = Manifest.model_json_schema()
    payload = {"manifests": names, "json_schema": schema}
    if out is not None:
        out.write_text(json.dumps(payload, indent=2))
        rprint(f"wrote {out}")
    else:
        rprint(json.dumps({"manifests": names}, indent=2))


@app.command("validate-manifest")
def validate_manifest_cmd(
    path: Path = typer.Argument(..., help="Path to a felix/v1 Agent YAML or JSON file."),
    environment: str = typer.Option(
        "development",
        "--environment",
        "-e",
        help="Assumed FELIX_ENVIRONMENT for governance checks.",
    ),
) -> None:
    """Validate a manifest schema + opt-in governance frameworks (GitOps CI)."""
    from felix.config import Settings
    from felix.manifests.governance import GovernanceError, validate_governance
    from felix.manifests.loader import load_manifest_file
    from felix.patterns.registry import list_patterns
    from felix.session.store import validate_checkpointer_config

    _load_plugins()
    settings = Settings(environment=environment)  # type: ignore[arg-type]
    try:
        manifest = load_manifest_file(path)
        validate_governance(manifest, settings)
        # The registry is open, so this is the only place a bad pattern name can be
        # caught before build time.
        pattern = manifest.spec.pattern
        if pattern not in list_patterns():
            known = ", ".join(sorted(list_patterns()))
            raise ValueError(f"unknown pattern {pattern!r} (registered: {known})")
        # Same reason: `checkpointer` is an open string resolved against a registry,
        # so an unknown one is only catchable here or at build time.
        validate_checkpointer_config(
            manifest.spec.memory.checkpointer,
            session_strategy=manifest.spec.session.strategy,
            compact_after_turn=manifest.spec.session.compact_after_turn,
            memory_capture=manifest.spec.memory.capture.enabled,
            memory_recall_tools=manifest.spec.memory.recall.tools,
        )
    except GovernanceError as exc:
        rprint(f"[red]governance fail[/red] {path}: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:
        rprint(f"[red]invalid[/red] {path}: {exc}")
        raise SystemExit(1) from exc
    rprint(f"[green]ok[/green] {path} ({manifest.metadata.name})")


@app.command("doctor")
def doctor_cmd() -> None:
    """Check runtime configuration (read-only)."""
    import asyncio
    from pathlib import Path as P

    from felix.config import get_settings

    settings = get_settings()
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]ok[/green]" if passed else "[red]FAIL[/red]"
        if not passed:
            ok = False
        suffix = f" — {detail}" if detail else ""
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
    if settings.auth_mode == "none":
        from felix.config import _is_loopback_host

        check(
            "allow_insecure (required for auth_mode=none outside loopback)",
            settings.allow_insecure or settings.environment == "development",
            f"allow_insecure={settings.allow_insecure}",
        )
        check(
            "auth_mode=none binds loopback only",
            _is_loopback_host(settings.host),
            f"host={settings.host}",
        )
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
                check("database", True, "reachable")

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

        # Redis / Valkey
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url)
            await client.ping()
            await client.aclose()
            check("redis", True, "reachable")
        except Exception as exc:
            check("redis", False, str(exc)[:120])

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

    asyncio.run(run_worker(get_settings()))


if __name__ == "__main__":
    app()
