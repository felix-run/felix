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
) -> None:
    """Run an offline eval against a dataset."""
    import asyncio

    from felix.config import get_settings
    from felix.eval.runner import start_run

    settings = get_settings()

    async def _run() -> None:
        result = await start_run(
            settings,
            tools=None,
            tenant_id=tenant,
            dataset_name=dataset,
            candidate_manifest=manifest,
        )
        rprint(result)

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


if __name__ == "__main__":
    app()
