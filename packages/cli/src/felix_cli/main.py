"""Felix CLI — migrate, eval, mint-jwt, login, ingest-docs, skills, bundle-manifests, version."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich import print as rprint

from felix_cli import __version__
from felix_cli.skills import skills_app

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
    # rather than at import, so importing this module for a helper stamps nothing. Every
    # subcommand does a thing and exits; a long-lived one (there was `temporal-worker`) would
    # need to skip this and stamp its own role, since `stamp_process_role` is first-write-wins.
    get_settings().stamp_process_role("cli")


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
    down: bool = typer.Option(
        False,
        "--down",
        help="Downgrade to REVISION. Destructive: each step's downgrade() drops what it added.",
    ),
) -> None:
    """Apply Alembic migrations, or with --down, roll them back to REVISION."""
    import asyncio

    from alembic import command
    from felix.config import get_settings
    from felix.db.migrations import alembic_config, passed_revision
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

    cfg = alembic_config()
    if down:
        command.downgrade(cfg, revision)
        rprint(f"[yellow]downgraded to {revision}[/yellow]")
        return
    # `command.upgrade` to a revision the database is already past does nothing and says
    # nothing, so `felix migrate 0021` on a database at 0023 printed "migrated to 0021" and
    # left it at 0023 — which is how the documented "prove the downgrade" step proved nothing.
    behind = asyncio.run(passed_revision(get_settings(), revision))
    if behind is not None:
        typer.echo(
            f"The database is at {behind}, past {revision}: upgrading would change nothing. "
            f"To roll back, run `felix migrate {revision} --down` (destructive).",
            err=True,
        )
        raise typer.Exit(2)
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
    strict_judge: bool = typer.Option(
        False,
        "--strict-judge",
        help="Exit 1 when an LLM judge could not run and an item was scored by the heuristic.",
    ),
) -> None:
    """Run an offline eval against a dataset."""
    import asyncio

    from felix.config import get_settings
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run
    from felix.eval.validation import validate_items

    _load_plugins()
    settings = get_settings()

    async def _run() -> None:
        name = dataset
        if fixture is not None:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            name = str(payload.get("name") or dataset)
            # Before the write, not after: an item whose prompt key is misspelled is stored
            # with an empty prompt and then scored by the non-empty rule, so the run passes
            # and means nothing. Exit 2 — a usage error, distinct from the exit 1 that means
            # the eval ran and items failed, which is what CI reads.
            report = validate_items(list(payload.get("items") or []))
            for warning in report.warnings:
                typer.echo(f"warning: {fixture}: {warning}", err=True)
            if not report.ok:
                for problem in report.errors:
                    typer.echo(f"error: {fixture}: {problem}", err=True)
                raise SystemExit(2)
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
        fallbacks = int((result.get("stats") or {}).get("judge_fallbacks") or 0)
        if fallbacks:
            # stderr, like every other line that is not the result. A judge that could not run
            # scored with the heuristic: the run passed a weaker test than it asked for.
            typer.echo(
                f"warning: {fallbacks} item(s) were scored by the heuristic because the LLM judge "
                "could not run; see judge_error on each score",
                err=True,
            )
        fails = int(result.get("fail_count") or 0)
        if fails or (strict_judge and fallbacks):
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


@app.command("login")
def login(
    url: str = typer.Option("http://localhost:8080", "--url", help="The Felix server to log in to."),
    tenant: str | None = typer.Option(
        None, "--tenant", "-t", help="Required when your GitHub orgs map to more than one tenant."
    ),
    save: bool = typer.Option(
        False, "--save", help="Keep the token in ~/.config/felix/token (0600) instead of printing it."
    ),
    insecure: bool = typer.Option(
        False,
        "--insecure",
        help="Allow plain http:// to a non-loopback server (the token crosses in cleartext).",
    ),
    github_actions: bool = typer.Option(
        False,
        "--github-actions",
        help="In a GitHub Actions job (permissions: id-token: write): trade the job's OIDC token "
        "instead of asking a person to approve a code.",
    ),
    audience: str | None = typer.Option(
        None,
        "--audience",
        help="With --github-actions: the server's FELIX_GITHUB_OIDC_AUDIENCE, if it is not --url.",
    ),
) -> None:
    """Log in to a Felix server with GitHub and print (or --save) a bearer token.

    Shows a code to enter on github.com — from any device, so this works over SSH — and waits
    for the approval. Needs the server to have FELIX_GITHUB_CLIENT_ID set. With
    --github-actions it asks no one: the job's OIDC token is the credential, and the server
    needs FELIX_GITHUB_OIDC_AUDIENCE instead.
    """
    import asyncio
    from urllib.parse import urlsplit

    from felix_client.login import (
        DeviceCode,
        LoginError,
        TokenFileError,
        github_actions_login,
        github_device_login,
        save_token,
    )

    if audience and not github_actions:
        typer.echo("--audience only applies with --github-actions", err=True)
        raise typer.Exit(2)
    if audience and urlsplit(audience).netloc != urlsplit(url).netloc:
        # The ID token is good at the audience's server for its few minutes of life, and it is
        # about to be handed to --url's. That is fine when both name one server; check it.
        typer.echo(f"warning: the ID token for {audience} is being sent to {url}", err=True)

    def show(code: DeviceCode) -> None:
        # stderr, so `TOKEN=$(felix login)` captures the token and nothing else. The server
        # relays GitHub's URL; one pointing elsewhere is the server's word against GitHub's.
        if urlsplit(code.verification_uri).hostname != "github.com":
            typer.echo(f"warning: {url} sent a verification URL that is not on github.com", err=True)
        typer.echo(f"Open {code.verification_uri} and enter {code.user_code}", err=True)
        typer.echo(f"Waiting for approval (the code expires in {code.expires_in // 60} min)…", err=True)

    try:
        if github_actions:
            flow = github_actions_login(url, audience=audience, tenant=tenant, allow_insecure=insecure)
        else:
            flow = github_device_login(url, tenant=tenant, on_code=show, allow_insecure=insecure)
        token = asyncio.run(flow)
    except LoginError as exc:
        if exc.code == "tenant_ambiguous":
            who = "This workflow is granted" if github_actions else "Your GitHub orgs map to"
            typer.echo(
                f"{who} more than one tenant ({', '.join(exc.tenants)}). "
                "Run again with --tenant <one of them>.",
                err=True,
            )
            raise typer.Exit(2) from exc
        typer.echo(f"login failed ({exc.code}): {exc}", err=True)
        raise typer.Exit(1) from exc

    if save:
        try:
            path = save_token(token)
        except TokenFileError as exc:
            typer.echo(f"not saved: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Logged in to tenant {token.tenant}; token saved to {path}.", err=True)
        return
    typer.echo(f"Logged in to tenant {token.tenant}.", err=True)
    if github_actions and os.environ.get("GITHUB_ACTIONS") == "true":
        # Registered with the job log's masking before it is printed, in case stdout is not
        # captured. On stderr, so `TOKEN=$(felix login --github-actions)` still gets the token
        # alone; capture it and `::add-mask::` it yourself too.
        typer.echo(f"::add-mask::{token.access_token}", err=True)
    # The token is the whole of stdout, unwrapped, for the same reason as `mint-jwt`.
    typer.echo(token.access_token)


@app.command("ingest-docs")
def ingest_docs(
    root: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory of .md/.mdx pages."),
    site_url: str = typer.Option(
        ...,
        "--site-url",
        help="The site those pages are published at; each document's source is its page URL there.",
    ),
    url: str = typer.Option("http://localhost:8080", "--url", help="The Felix server to ingest into."),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="FELIX_API_KEY",
        help="A key or token with documents:write. Defaults to the token `felix login --save` kept.",
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Delete documents under --site-url that no page produced any more."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would change; change nothing."),
    max_prune: int = typer.Option(
        10,
        "--max-prune",
        min=0,
        help="With --prune: delete nothing if more than this many documents would go.",
    ),
) -> None:
    """Sync a directory of Markdown/MDX pages into the server's document corpus.

    One document per page, sourced at its public URL, so an agent can quote a hit and fetch the
    page. Safe to repeat: a page sent again replaces itself.
    """
    import asyncio

    from felix_client import FelixClient
    from felix_client.docs_sync import SiteUrlError, read_pages, sync_pages

    try:
        pages, skipped = read_pages(root, site_url=site_url)
    except SiteUrlError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for path, why in skipped:
        typer.echo(f"skip {path}: {why}", err=True)
    if not pages:
        typer.echo(f"no .md/.mdx pages under {root}", err=True)
        raise typer.Exit(1)
    client = FelixClient.from_login(url, api_key=api_key)
    result = asyncio.run(
        sync_pages(client, pages, site_url=site_url, prune=prune, dry_run=dry_run, max_prune=max_prune)
    )
    verb = "would ingest" if dry_run else "ingested"
    for source, chunks in result.ingested:
        typer.echo(f"{verb} {source}" + ("" if dry_run else f" ({chunks} chunks)"))
    for source in result.pruned:
        typer.echo(f"{'would prune' if dry_run else 'pruned'} {source}")
    for source, error in result.failed:
        typer.echo(f"failed {source}: {error}", err=True)
    typer.echo(
        f"{len(result.ingested)} pages, {len(result.pruned)} pruned, {len(result.failed)} failed", err=True
    )
    if result.failed:
        raise typer.Exit(1)


# `felix skills browse|add`: their own module, since they talk to a server, as `ingest-docs` does.
app.add_typer(skills_app, name="skills")


workspace_app = typer.Typer(name="workspace", help="Manage FELIX_WORKSPACE_ROOT.", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")


@workspace_app.command("migrate")
def workspace_migrate(
    tenant: str = typer.Option("default", "--tenant", help="Whose `shared` scope receives the files."),
    keep: list[str] = typer.Option(
        [], "--keep", help="A top-level name to leave at the root (repeatable), e.g. AGENTS.md."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would move; move nothing."),
) -> None:
    """Move files written before workspace scopes into a tenant's `shared` scope.

    Before scoping, every agent worked at the root of FELIX_WORKSPACE_ROOT. Afterwards only the
    operator's `deployment`-scope manifests see the root, so this carries what was there to where
    a `scope: tenant` manifest of TENANT finds it. Nothing is overwritten; running it twice is
    harmless. Run it on the host (or in a container) that mounts the workspace, once per upgrade.
    """
    from felix.config import get_settings
    from felix.tools.workspace import deployment_workspace
    from felix.tools.workspace_scope import migrate_legacy_files

    raw = (get_settings().workspace_root or "").strip()
    if not raw:
        rprint("[red]FELIX_WORKSPACE_ROOT is not set; nothing to migrate.[/red]")
        raise typer.Exit(2)
    try:
        base = deployment_workspace(raw)
        result = migrate_legacy_files(base, tenant, keep=frozenset(keep), dry_run=dry_run)
    except (ValueError, OSError) as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    verb = "would move" if dry_run else "moved"
    rprint(f"{verb} {len(result.moved)} entr{'y' if len(result.moved) == 1 else 'ies'} into {result.target}")
    for name in result.moved:
        rprint(f"  {name}")
    if result.kept:
        rprint(f"kept at the root: {', '.join(result.kept)}")
    if result.collided:
        rprint(
            f"[yellow]left at the root, already present in {result.target}: "
            f"{', '.join(result.collided)}[/yellow]"
        )
        raise typer.Exit(1)


@workspace_app.command("upload")
def workspace_upload(
    tenant: str = typer.Option(None, "--tenant", help="Only this tenant's scopes."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would be copied; copy nothing."),
) -> None:
    """Copy local workspace scopes into their hosted sandboxes, once, after turning on `hosted`.

    A scope starts empty under FELIX_WORKSPACE_BACKEND=hosted; this carries each local scope's files
    (`<root>/.felix-scopes/<tenant>/<key>`) into the sandbox of the same scope and backs it up. The
    local files are left in place. Run it where the workspace is mounted, with the hosted settings.
    """
    import asyncio

    from felix.config import get_settings
    from felix.tools.workspace_hosted import upload_local_scopes

    settings = get_settings()
    if settings.workspace_backend != "hosted":
        rprint("[red]FELIX_WORKSPACE_BACKEND is not `hosted`: there is nothing to upload to.[/red]")
        raise typer.Exit(2)
    try:
        settings.validate_runtime()
        reports = asyncio.run(upload_local_scopes(settings, tenant=tenant, dry_run=dry_run))
    except (ValueError, OSError, RuntimeError) as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    verb = "would copy" if dry_run else "copied"
    skipped = 0
    for report in reports:
        rprint(
            f"{report.scope}: {verb} {len(report.uploaded)} file(s)"
            + (", backed up" if report.checkpointed else "")
        )
        for path, why in report.skipped:
            skipped += 1
            rprint(f"  [yellow]skipped {path}: {why}[/yellow]")
    if not reports:
        rprint("no local scopes to copy")
    if skipped:
        raise typer.Exit(1)


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


def _skill_import_check_notes(settings: Settings) -> list[str]:
    """Notes, never failures, on the worker's periodic skill-import checks
    (`FELIX_SKILL_IMPORT_CHECK_HOURS`). The sweep runs in the worker with the worker's own
    environment, so an allowlist or a token set only on the API is one the sweep never sees --
    and doctor, run on either, can only say what this process has."""
    if not settings.skill_import_check_hours:
        return []
    notes = [
        f"skill import checks every {settings.skill_import_check_hours}h run in the worker: give it "
        "the same FELIX_SKILL_IMPORT_SOURCES and FELIX_SKILL_IMPORT_GITHUB_TOKEN as the API"
    ]
    if not settings.skill_import_sources.strip():
        notes.append(
            "no FELIX_SKILL_IMPORT_SOURCES here: checks re-resolve any GitHub origin an import stored"
        )
    if not settings.skill_import_github_token:
        notes.append(
            "no FELIX_SKILL_IMPORT_GITHUB_TOKEN here: checks read GitHub anonymously, 60 calls an hour "
            "for the whole server, and private origins are not found"
        )
    return notes


def _skill_update_webhook_notes(settings: Settings) -> list[str]:
    """A note, never a failure, when `FELIX_SKILL_UPDATE_WEBHOOKS` is set. Checks run on the API and
    on the worker and only queue the event; the worker sends it and re-checks the binding first.
    An API without the binding queues nothing, and a worker without it marks the endpoint dead --
    and doctor, run on either, can only see this process's environment."""
    if not settings.skill_update_webhooks.strip():
        return []
    return [
        "skill update webhooks are queued by checks on the API and the worker and sent by the worker: "
        "give both the same FELIX_SKILL_UPDATE_WEBHOOKS and FELIX_WEBHOOK_ENDPOINTS"
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
    for note in _skill_import_check_notes(settings) + _skill_update_webhook_notes(settings):
        rprint(f"  [yellow]note[/yellow]  {note}")
    if settings.environment == "development":
        rprint("  [dim]posture[/dim]  production posture checks skipped — FELIX_ENVIRONMENT=development")
    from felix.security.stdio_policy import allowed_commands, describe_allowlist

    # Not a failure either way — stdio off is the safe default; on is a deliberate choice.
    rprint(
        f"  [green]ok[/green]  mcp stdio — {describe_allowlist(settings)}"
        + ("" if allowed_commands(settings) else " (safe default)")
    )
    from felix.security import shell_policy

    rprint(
        f"  [green]ok[/green]  shell tools — {shell_policy.describe_allowlist(settings)}"
        + ("" if shell_policy.allowed_prefixes(settings) else " (safe default)")
    )
    from felix.patterns.model_vision import vision_plan

    # A note, not a failure: a text-only default is a legal, cheap choice. What doctor owes the
    # operator is that every image sent to it will be refused until a vision route is named.
    # A *misconfigured* vision route is a failure: every manifest without its own model fails
    # to build, as an unroutable fallback does.
    image_plan = vision_plan(settings, None)
    if image_plan.misconfigured:
        check("default vision route", False, image_plan.problem or "")
    elif image_plan.problem:
        rprint(f"  [yellow]note[/yellow]  images — {image_plan.problem}; turns carrying one get a 422")
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
        from felix.config import redis_url_in_use

        if settings.redis_url.strip() and not redis_url_in_use(settings):
            # The `/ready` rule: under memory:// the default URL is a placeholder, not a
            # configured Redis, and every store is process-local anyway.
            check(
                redis_label,
                True,
                "not in use — memory:// with FELIX_REDIS_URL unset; set it to require one",
            )
        elif not settings.redis_url.strip():
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


if __name__ == "__main__":
    app()
