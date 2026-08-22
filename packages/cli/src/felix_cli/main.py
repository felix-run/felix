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
) -> None:
    """Run an offline eval against a dataset."""
    import asyncio

    from felix.config import get_settings
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

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
            tools=None,
            tenant_id=tenant,
            dataset_name=name,
            candidate_manifest=manifest,
            mock=mock,
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
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Write JSON Schema / bundle summary here."
    ),
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
    check("auth_mode", settings.auth_mode in {"none", "api_key", "jwt"}, settings.auth_mode)
    if settings.auth_mode == "none":
        check(
            "allow_insecure (required for auth_mode=none outside loopback)",
            settings.allow_insecure or settings.environment == "development",
            f"allow_insecure={settings.allow_insecure}",
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
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                check("database", True, "reachable")
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
