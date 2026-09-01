"""Outbound egress and sandbox confinement.

The SSRF guard checked private/loopback/link-local ranges **only when the hostname was
already an IP literal**, so any DNS name resolving to 169.254.169.254 sailed through —
the standard cloud-metadata path. The browser tool then checked its URL once and handed
it to Chromium, which follows redirects and subresources. And the Docker sandbox called
the synchronous docker SDK from a coroutine, so its own timeout could never fire and the
whole API event loop stalled for the container's lifetime.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest
from felix.security import ssrf
from felix.security.ssrf import assert_safe_outbound_url as chk

# --- literal addresses ----------------------------------------------------------


@pytest.mark.parametrize(
    "url,label",
    [
        ("https://169.254.169.254/latest/meta-data/", "link-local metadata"),
        ("https://[::ffff:169.254.169.254]/", "IPv4-mapped metadata"),
        ("https://[::ffff:127.0.0.1]/", "IPv4-mapped loopback"),
        ("https://2130706433/", "integer-form loopback"),
        ("https://10.0.0.5/", "RFC1918"),
        ("https://192.168.1.1/", "RFC1918"),
        ("https://100.64.0.1/", "carrier-grade NAT"),
        ("https://224.0.0.1/", "multicast"),
        ("https://0.0.0.0/", "unspecified"),
    ],
)
def test_blocked_literals(url: str, label: str) -> None:
    with pytest.raises(ValueError):
        chk(url)


@pytest.mark.parametrize(
    "host",
    ["kubernetes.default", "metadata.google.internal", "instance-data", "consul"],
)
def test_blocked_internal_names(host: str) -> None:
    with pytest.raises(ValueError, match="internal"):
        chk(f"https://{host}/")


@pytest.mark.parametrize("host", ["a.svc", "b.cluster.local", "c.internal", "d.local"])
def test_blocked_internal_suffixes(host: str) -> None:
    with pytest.raises(ValueError, match="internal"):
        chk(f"https://{host}/")


# --- the actual gap: DNS ---------------------------------------------------------


def test_hostname_resolving_to_metadata_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the bypass. No literal IP appears anywhere in the URL."""
    monkeypatch.setattr(ssrf, "resolve_host", lambda h: ["169.254.169.254"])
    with pytest.raises(ValueError, match="resolves to a blocked address"):
        chk("https://imds.attacker.example/latest/meta-data/")


def test_hostname_resolving_to_private_space_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda h: ["10.1.2.3"])
    with pytest.raises(ValueError, match="resolves to a blocked address"):
        chk("https://internal.nip.io/")


def test_any_blocked_address_in_the_set_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A split-horizon name must not pass because one record happens to be public."""
    monkeypatch.setattr(ssrf, "resolve_host", lambda h: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(ValueError):
        chk("https://mixed.example/")


def test_public_hostname_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda h: ["93.184.216.34"])
    chk("https://example.com/")


def test_unresolvable_host_does_not_hard_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing every DNS failure would make the harness brittle offline; the
    connection itself will fail anyway."""
    monkeypatch.setattr(ssrf, "resolve_host", lambda h: [])
    chk("https://nonexistent.invalid/")


def test_resolution_can_be_skipped_for_prevalidated_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _boom(h: str) -> list[str]:
        called["n"] += 1
        return []

    monkeypatch.setattr(ssrf, "resolve_host", _boom)
    chk("https://example.com/", resolve=False)
    assert called["n"] == 0


def test_scheme_is_still_enforced() -> None:
    with pytest.raises(ValueError, match="scheme"):
        chk("file:///etc/passwd")
    with pytest.raises(ValueError, match="http urls blocked"):
        chk("http://example.com/")


# --- where resolution happens ----------------------------------------------------


def test_manifest_parsing_never_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validators do the syntactic checks only.

    `resolve_host` is a blocking `getaddrinfo`, and it ran inside a pydantic validator —
    on the API event loop — once per ref, on every manifest read and every write. With the
    ref lists capped at 64 that was still seconds of stall from one request, and against a
    nameserver that drops queries rather than answering, far worse.
    """
    from felix.manifests.schema import Spec
    from felix.security import ssrf

    calls: list[str] = []
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: calls.append(host) or [])

    spec = Spec(
        pattern="react",
        mcp_servers=[{"name": f"m{i}", "url": f"https://mcp-{i}.example.com/x"} for i in range(8)],
        peers=[{"name": "p", "url": "https://peer.example.com"}],
        containers=[{"name": "c", "gateway_url": "https://gw.example.com", "image": "i"}],
    )
    assert len(spec.mcp) == 8
    assert calls == [], f"parsing resolved {len(calls)} hostname(s)"


def test_parsing_still_rejects_what_needs_no_lookup() -> None:
    """Dropping resolution must not drop the checks that never needed it."""
    from felix.manifests.schema import McpServerRef
    from pydantic import ValidationError

    for bad in (
        "https://127.0.0.1/mcp",  # literal loopback
        "https://2130706433/mcp",  # decimal form of the same
        "https://metadata.google.internal/mcp",  # internal name
        "https://thing.cluster.local/mcp",  # internal suffix
        "http://example.com/mcp",  # plain http outside development
        "ftp://example.com/mcp",  # scheme
    ):
        with pytest.raises(ValidationError):
            McpServerRef(name="x", url=bad)


@pytest.mark.asyncio
async def test_dial_resolves_and_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authoritative check moved to dial, so it must actually fire there.

    This is also the only placement that catches a name which re-resolves to private space
    after the manifest was validated — the check at write time never could.
    """
    from felix.security import ssrf
    from felix.security.ssrf import EgressBlocked
    from felix.tools.transports import HttpExecutor

    ex = HttpExecutor("https://rebinds.example.com/hook")
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["169.254.169.254"])
    with pytest.raises(EgressBlocked) as exc:
        await ex.execute({"x": 1})
    # The reason names the resolved IP, and at dial time it would land in a tool message the
    # model reads — turning any peer/container/MCP ref into an internal-DNS oracle.
    assert "169.254.169.254" not in str(exc.value)
    assert "rebinds.example.com" not in str(exc.value)


@pytest.mark.asyncio
async def test_dial_resolution_does_not_stall_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`getaddrinfo` is synchronous, so the dial-time check has to leave the loop.

    Otherwise the fix just moves the stall from the parse path to the tool-call path.
    """
    from felix.security import ssrf
    from felix.security.ssrf import assert_safe_outbound_url_async

    def _slow(host: str) -> list[str]:
        time.sleep(0.4)
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "resolve_host", _slow)

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(_heartbeat())
    await assert_safe_outbound_url_async("https://slow.example.com/x")
    hb.cancel()
    assert ticks >= 3, "the event loop was blocked during DNS resolution"


# --- browser egress guard -------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_registers_an_egress_guard() -> None:
    """page.goto() follows redirects and subresources past the initial check."""
    from felix.tools.browser import _BrowserExecutor

    ex = _BrowserExecutor(op="content", timeout_ms=1000, path_prefix="", allow_http=False, binding="chromium")

    class _Page:
        def __init__(self) -> None:
            self.handler = None

        async def route(self, pattern: str, handler):
            self.handler = handler

    page = _Page()
    await ex._install_egress_guard(page)
    assert page.handler is not None, "no interceptor installed"

    aborted: list[str] = []
    continued: list[str] = []

    class _Route:
        def __init__(self, url: str) -> None:
            self.url = url

        async def abort(self) -> None:
            aborted.append(self.url)

        async def continue_(self) -> None:
            continued.append(self.url)

    class _Req:
        def __init__(self, url: str) -> None:
            self.url = url

    # a redirect hop to the metadata service must be aborted
    r = _Route("https://169.254.169.254/latest/meta-data/")
    await page.handler(r, _Req(r.url))
    assert aborted == [r.url]

    ok = _Route("https://example.com/style.css")
    await page.handler(ok, _Req(ok.url))
    assert continued == [ok.url]


@pytest.mark.asyncio
async def test_path_prefix_applies_to_navigation_not_subresources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcing the prefix on every asset would break any real page."""
    from felix.security import ssrf
    from felix.tools.browser import _BrowserExecutor

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["93.184.216.34"])

    ex = _BrowserExecutor(
        op="content",
        timeout_ms=1000,
        path_prefix="https://docs.example.com",
        allow_http=False,
        binding="chromium",
    )
    with pytest.raises(ValueError, match="must start with"):
        await ex._check_url("https://other.example.com/x")
    # subresource check is SSRF-only
    await ex._check_egress("https://cdn.example.com/app.js")


# --- sandbox --------------------------------------------------------------------


# The bug this guards against: `input=` was passed to `containers.run()` for as long as the
# sandbox existed, so every call failed against a real daemon. It survived because the fake
# below accepts any kwarg — the suite pinned the confinement flags but never the call
# signature. Derive the accepted set from docker-py's own constants; a hand-copied list
# drifts from the SDK and re-opens the same gap.
#
# Captured at import, before any test stubs sys.modules["docker"]. None when the sandbox
# extra is absent, in which case the fake skips the check — the gated test below is the
# arm that must not silently vanish, so it uses require_optional rather than this.
def _real_docker_run_kwargs() -> frozenset[str] | None:
    try:
        from docker.models.containers import RUN_CREATE_KWARGS, RUN_HOST_CONFIG_KWARGS
    except ImportError:
        return None
    # `run()` consumes these four itself rather than forwarding them.
    return frozenset({*RUN_CREATE_KWARGS, *RUN_HOST_CONFIG_KWARGS, "command", "remove", "stdout", "stderr"})


_VALID_DOCKER_RUN_KWARGS = _real_docker_run_kwargs()


def _stub_docker(sleep_s: float) -> None:
    fake = types.ModuleType("docker")

    class _Containers:
        last_kwargs: dict = {}

        def run(self, *a, **k):
            # Reject what a real daemon would reject, so the fake cannot bless a call
            # signature docker-py does not accept.
            if _VALID_DOCKER_RUN_KWARGS is not None:
                unknown = set(k.keys()) - _VALID_DOCKER_RUN_KWARGS
                if unknown:
                    raise TypeError(f"run() got an unexpected keyword argument {unknown.pop()!r}")
            _Containers.last_kwargs = k
            time.sleep(sleep_s)
            return b"ok"

    class _Client:
        containers = _Containers()

    fake.from_env = lambda: _Client()  # type: ignore[attr-defined]
    sys.modules["docker"] = fake


@pytest.mark.asyncio
async def test_sandbox_timeout_can_actually_fire() -> None:
    """asyncio.wait_for cannot interrupt a blocking C call, so the declared sandbox
    timeout never worked."""
    _stub_docker(2.0)
    from felix.tools.transports import SandboxExecutor

    ex = SandboxExecutor()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ex.execute({"code": "x"}), timeout=0.4)


@pytest.mark.asyncio
async def test_sandbox_does_not_stall_the_event_loop() -> None:
    """A model emitting `while True: pass` froze every concurrent request."""
    _stub_docker(0.6)
    from felix.tools.transports import SandboxExecutor

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(_heartbeat())
    await SandboxExecutor().execute({"code": "x"})
    hb.cancel()
    assert ticks >= 3, "the event loop was blocked during the container run"


@pytest.mark.asyncio
async def test_sandbox_is_confined() -> None:
    _stub_docker(0.0)
    from felix.tools.transports import SandboxExecutor

    await SandboxExecutor().execute({"code": "x"})
    kwargs = sys.modules["docker"].from_env().containers.last_kwargs
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["read_only"] is True
    assert kwargs["user"] != "root" and kwargs["user"] != ""
    assert kwargs["pids_limit"] > 0
    assert kwargs["nano_cpus"] > 0
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["network_disabled"] is True


@pytest.mark.asyncio
async def test_sandbox_uses_environment_not_stdin() -> None:
    """The sandbox passes JSON via environment variable, not stdin."""
    _stub_docker(0.0)
    from felix.tools.transports import SandboxExecutor

    await SandboxExecutor().execute({"test": "data"})
    kwargs = sys.modules["docker"].from_env().containers.last_kwargs
    assert "environment" in kwargs
    assert "FELIX_SANDBOX_INPUT" in kwargs["environment"]
    assert "input" not in kwargs, "docker-py doesn't support 'input' parameter"


@pytest.mark.asyncio
async def test_sandbox_kwargs_are_accepted_by_real_docker_py() -> None:
    """Every kwarg the executor sends must be one docker-py actually forwards.

    The fake cannot prove this on its own — it is only as strict as the list it is given.
    This arm reads the SDK, so a kwarg that docker-py drops or renames fails here rather
    than in production.
    """
    from tests.optional_deps import require_optional

    models = require_optional("docker.models.containers", "sandbox")
    accepted = {
        *models.RUN_CREATE_KWARGS,
        *models.RUN_HOST_CONFIG_KWARGS,
        "command",
        "remove",
        "stdout",
        "stderr",
    }
    _stub_docker(0.0)
    from felix.tools.transports import SandboxExecutor

    await SandboxExecutor().execute({"k": "v"})
    sent = set(sys.modules["docker"].from_env().containers.last_kwargs)
    assert not sent - accepted, f"docker-py would reject: {sorted(sent - accepted)}"


# --- sandbox image allowlist -----------------------------------------------------


def test_default_image_is_always_allowed() -> None:
    from felix.tools.sandboxes import DEFAULT_SANDBOX_IMAGE, assert_sandbox_image_allowed

    assert_sandbox_image_allowed(DEFAULT_SANDBOX_IMAGE, _settings())


def test_arbitrary_image_is_rejected() -> None:
    from felix.tools.sandboxes import SandboxImageNotAllowed, assert_sandbox_image_allowed

    with pytest.raises(SandboxImageNotAllowed):
        assert_sandbox_image_allowed("attacker/miner:latest", _settings())


def test_operator_can_allow_more_images() -> None:
    from felix.tools.sandboxes import assert_sandbox_image_allowed

    assert_sandbox_image_allowed(
        "ghcr.io/me/runner:1", _settings(sandbox_allowed_images="ghcr.io/me/runner:1")
    )


def _settings(**kw):
    from felix.config import Settings

    base = {
        "database_url": "memory://sbx",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
    }
    base.update(kw)
    return Settings(**base)


@pytest.mark.asyncio
async def test_a_slow_resolver_is_refused_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup that outruns its budget blocks, rather than falling through.

    The guard is advisory — httpx resolves again at connect — so letting a timeout pass
    would hand a selectively-slow nameserver the exact bypass the guard exists to close.
    """
    from felix.security import ssrf
    from felix.security.ssrf import EgressBlocked, assert_safe_outbound_url_async

    monkeypatch.setattr(ssrf, "_RESOLVE_BUDGET_S", 0.1)
    # Kept short: `wait_for` frees the caller but cannot cancel the thread, so the sleep is
    # still running at teardown — which is the resource cost the budget exists to bound.
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: (time.sleep(0.6), ["93.184.216.34"])[1])

    with pytest.raises(EgressBlocked):
        await assert_safe_outbound_url_async("https://blackhole.example.com/x")
