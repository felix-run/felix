"""Boot-time refusals for configuration that silently weakened authentication.

Each of these used to start cleanly and then do something its operator did not ask for: a plugin
mount of `""` made every path anonymous, a malformed `FELIX_AUTH_API_KEYS` became "no keys" with
no log line, `"scopes": "admin"` granted the scopes `a`, `d`, `m`, `i`, `n`, and a trigger claim
could expire while the delivery it guarded still verified.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.a2a.server import A2A_TASK_FAILED, handle_rpc
from felix.auth.context import ANONYMOUS
from felix.auth.middleware import authenticate_request
from felix.config import MIN_API_KEY_CHARS, Settings
from felix.tools.provider import InMemoryToolProvider
from starlette.requests import Request

from tests.support.factories import make_settings
from tests.support.mgmt_keys import LONG_KEY, long_keys
from tests.support.optional_deps import require_optional


def _request(path: str, *, bearer: str = "", root_path: str = "") -> Request:
    headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "root_path": root_path,
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("t", 80),
        }
    )


def _status(result: Any) -> int | None:
    return getattr(result, "status_code", None)


# --- L1: self-authenticating mounts ------------------------------------------------------------


class _MountPlugin:
    def __init__(self, *mounts: object, name: str = "planted") -> None:
        self.name = name
        self._mounts = mounts

    @property
    def self_authenticating_mounts(self) -> tuple[object, ...]:
        return self._mounts


def _boot(*plugins: Any) -> Any:
    from felix_api.app import create_app

    return create_app(settings=make_settings(host="127.0.0.1"), plugins=list(plugins))


@pytest.mark.parametrize(
    ("mount", "why"),
    [
        ("", "the empty mount matched every path"),
        ("/", "the root"),
        ("chat", "not a path"),
        ("/chat", "a core route"),
        ("/chat/hooks", "under a core route"),
        ("/v1", "the OpenAI surface"),
        ("/manifests", "management writes"),
        ("/skill-library/~me", "under a core prefix"),
        ("/auth", "a prefix of the login routes"),
        ("/.well-known/plugin", "under a public prefix"),
        ("/health", "a probe"),
        ("/triggers", "the trigger prefix"),
        ("/hooks/", "a trailing slash"),
        ("/hooks//x", "an empty segment"),
        ("/hooks/../chat", "a traversal"),
        ("/hooks x", "whitespace"),
    ],
)
def test_a_mount_that_overlaps_core_or_is_malformed_refuses_to_boot(mount: str, why: str) -> None:
    with pytest.raises(RuntimeError, match="plugin 'planted'") as raised:
        _boot(_MountPlugin(mount))
    assert repr(mount) in str(raised.value), why


def test_a_mount_that_is_not_a_string_refuses_to_boot() -> None:
    with pytest.raises(RuntimeError, match="is a bytes, not a path"):
        _boot(_MountPlugin(b"/hooks"))


def test_the_refusal_names_the_plugin_that_declared_the_bad_mount() -> None:
    with pytest.raises(RuntimeError) as raised:
        _boot(_MountPlugin("/hooks", name="good"), _MountPlugin("/chat", name="bad"))
    assert "plugin 'bad'" in str(raised.value)
    assert "'good'" not in str(raised.value)


def test_a_lookalike_of_a_core_root_is_its_own_segment() -> None:
    """`/chat-hooks` is not under `/chat`: the comparison is by segment, not by string prefix."""
    _boot(_MountPlugin("/chat-hooks"))


async def test_the_channels_mount_still_boots_and_opens_only_itself() -> None:
    channels = require_optional("felix_channels", "channels")
    plugin = channels.ChannelsPlugin()
    _boot(plugin)

    mounts = plugin.self_authenticating_mounts
    settings = _api_key_settings(long_keys())
    inside = await authenticate_request(
        _request("/channels/slack/events"), settings, self_authenticating_mounts=mounts
    )
    outside = await authenticate_request(_request("/chat"), settings, self_authenticating_mounts=mounts)
    assert inside is ANONYMOUS
    assert _status(outside) == 401


async def test_a_mount_is_matched_on_the_path_the_router_sees() -> None:
    """Behind an ASGI `root_path` (a proxy prefix), the router matches the path with it taken off,
    and so must the mount check -- or the two disagree about which route a request is for."""
    settings = _api_key_settings(long_keys())
    mounts = ("/hooks",)

    under = await authenticate_request(
        _request("/api/hooks/x", root_path="/api"), settings, self_authenticating_mounts=mounts
    )
    assert under is ANONYMOUS
    # Mounted under `/hooks`, the router routes this request as `/x`: the raw path only looks like
    # it is under the mount, and the route it reaches is not the plugin's.
    beside = await authenticate_request(
        _request("/hooks/x", root_path="/hooks"), settings, self_authenticating_mounts=mounts
    )
    assert _status(beside) == 401


def test_a_plugin_may_route_under_its_own_mount() -> None:
    """The check runs before plugin routers are added, so a plugin's own route is not a collision."""
    from fastapi import APIRouter

    class _Routed(_MountPlugin):
        def routes(self, app: Any, *, tools: Any) -> None:
            router = APIRouter()

            @router.post("/inbound")
            async def inbound() -> dict[str, str]:
                return {}

            app.include_router(router, prefix="/hooks")

    _boot(_Routed("/hooks"))


def test_the_mount_check_sees_every_core_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes a mount is checked against are the snapshot taken before plugin routers; with no
    plugin, that snapshot must be the app's whole route set. A core router included after the
    plugin loop would be missing from it, and a mount could claim its root."""
    import felix_api.app as app_module
    from fastapi.routing import iter_route_contexts
    from felix_api.main import create_application

    seen: list[str] = []

    def spy(mounts: Any, core_paths: Any) -> None:
        seen.extend(core_paths)

    monkeypatch.setattr(app_module, "installed_plugins", list)
    monkeypatch.setattr(app_module, "validate_self_authenticating_mounts", spy)
    app = create_application()

    final = [c.path for c in iter_route_contexts(app.routes) if c.path]
    assert seen, "the mount check never ran"
    assert sorted(seen) == sorted(final)


# --- L2: FELIX_AUTH_API_KEYS -------------------------------------------------------------------


def _api_key_settings(raw: str, **over: Any) -> Settings:
    return make_settings(auth_mode="api_key", auth_api_keys=raw, **over)


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        ('{"dev-key": {"scopes": ["admin"]', "not valid JSON"),
        ('["dev-key"]', "JSON object"),
        ('{"dev-key": "admin"}', "must be a JSON object"),
        ('{"dev-key": {"scopes": "admin"}}', "list of strings"),
        ('{"dev-key": {"scopes": ["admin", 7]}}', "list of strings"),
        ('{"dev-key": {"tenant_id": 7}}', '"tenant_id" must be a string'),
        ('{"dev-key": {"sub": ["ops"]}}', '"sub" must be a string'),
    ],
)
def test_a_malformed_key_list_refuses_to_boot(raw: str, says: str) -> None:
    with pytest.raises(RuntimeError, match=says) as raised:
        _api_key_settings(raw).validate_runtime()
    # Entries are named by position: the key is the credential, so it is never in the message.
    assert "dev-key" not in str(raised.value)


def test_a_malformed_key_list_is_inert_outside_api_key_mode() -> None:
    make_settings(auth_api_keys="{not json", host="127.0.0.1").validate_runtime()


def test_a_short_key_refuses_to_boot_outside_development_at_the_boundary() -> None:
    """Any environment but development -- staging here, not only production."""
    at_min = "test-key-" + "x" * (MIN_API_KEY_CHARS - len("test-key-"))
    short = at_min[:-1]
    assert len(at_min) == MIN_API_KEY_CHARS
    staging = {"environment": "staging", "redis_url": "redis://127.0.0.1:9/0"}

    with pytest.raises(RuntimeError, match=f"at least {MIN_API_KEY_CHARS}") as raised:
        _api_key_settings(json.dumps({short: {}}), **staging).validate_runtime()
    assert short not in str(raised.value)
    _api_key_settings(json.dumps({at_min: {}}), **staging).validate_runtime()

    # A local box keeps its short key.
    _api_key_settings(json.dumps({"dev-key": {}}), environment="development").validate_runtime()


def test_the_documented_sample_keys_boot() -> None:
    """`.env.example`'s commented sample and `scripts/dev-key.sh`'s shape, as an operator copies them."""
    from tests.support.paths import ROOT

    line = next(
        ln for ln in (ROOT / ".env.example").read_text().splitlines() if "FELIX_AUTH_API_KEYS={" in ln
    )
    sample = line.split("=", 1)[1]
    generated = json.dumps(
        {"sk-felix-local-" + "0" * 48: {"tenant_id": "default", "sub": "local-dev", "scopes": ["admin"]}}
    )
    _api_key_settings(sample).validate_runtime()
    _api_key_settings(generated).validate_runtime()


async def test_a_key_without_a_tenant_is_the_default_tenant_at_boot_and_at_the_door() -> None:
    settings = _api_key_settings(json.dumps({LONG_KEY: {"scopes": ["chat:write"]}}))
    settings.validate_runtime()

    auth = await authenticate_request(_request("/chat", bearer=LONG_KEY), settings)
    assert auth.principal.tenant_id == "default"  # type: ignore[union-attr]
    assert auth.principal.subject == "api_key"  # type: ignore[union-attr]
    assert auth.principal.scopes == frozenset({"chat:write"})  # type: ignore[union-attr]


async def test_a_string_scopes_value_authenticates_nobody_at_the_door() -> None:
    """Without the boot check (a caller that never ran `validate_runtime`), the door reads the same
    parser, refuses the list and fails closed -- not the five one-letter scopes it once granted."""
    from felix.auth.middleware import _parse_api_keys

    _parse_api_keys.cache_clear()
    raw = json.dumps({LONG_KEY: {"tenant_id": "default", "sub": "ops", "scopes": "admin"}})
    auth = await authenticate_request(_request("/chat", bearer=LONG_KEY), _api_key_settings(raw))

    assert _status(auth) == 401


@pytest.mark.parametrize(
    ("raw", "says"),
    [('{"dev-key": ', "not valid JSON"), ('["dev-key"]', "JSON object")],
)
def test_a_malformed_key_list_is_logged_when_parsed(
    caplog: pytest.LogCaptureFixture, raw: str, says: str
) -> None:
    from felix.auth.middleware import _parse_api_keys

    _parse_api_keys.cache_clear()
    with caplog.at_level("ERROR", logger="felix.auth.middleware"):
        assert _parse_api_keys(raw) == {}
    messages = [r.getMessage() for r in caplog.records]
    assert any(says in m for m in messages), messages
    assert not any("dev-key" in m for m in messages), messages


# --- L4: auth_mode=none ------------------------------------------------------------------------


def test_auth_mode_none_warns_on_every_accepted_boot(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="felix.config"):
        make_settings(host="127.0.0.1").validate_runtime()
    warned = [r.getMessage() for r in caplog.records if "unauthenticated" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "FELIX_HOST='127.0.0.1'" in warned[0], warned[0]

    caplog.clear()
    with caplog.at_level("WARNING", logger="felix.config"):
        _api_key_settings(long_keys()).validate_runtime()
    assert not [r for r in caplog.records if "unauthenticated" in r.getMessage()]


# --- L5a: trigger claims outlive the signature window ------------------------------------------


def _trigger_settings(ttl: int) -> Settings:
    spec = {"tenant": "acme", "manifest": "triage", "scheme": "github", "secret": "k" * 24, "prompt": "Go."}
    return make_settings(triggers=json.dumps({"t": spec}), idempotency_ttl_seconds=ttl, host="127.0.0.1")


def test_a_claim_shorter_than_the_replay_window_refuses_to_boot() -> None:
    from felix.triggers import TIMESTAMP_TOLERANCE_S

    window = 2 * TIMESTAMP_TOLERANCE_S
    with pytest.raises(RuntimeError, match="FELIX_IDEMPOTENCY_TTL_SECONDS"):
        _trigger_settings(window - 1).validate_runtime()
    _trigger_settings(window).validate_runtime()
    # Without triggers the TTL guards chat's Idempotency-Key alone, which has no signature window.
    make_settings(idempotency_ttl_seconds=1, host="127.0.0.1").validate_runtime()


# --- L5b: an A2A task's failure reason stays in the log ----------------------------------------


async def test_a_failed_a2a_task_stores_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "pw-" + "q" * 20
    leak = f"connection to db-internal.corp:5432 refused for felix_rw:{secret}"
    monkeypatch.setattr("felix.secrets._resolved_secret_values", [secret])

    async def _fail(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(leak)

    monkeypatch.setattr("felix.runtime.resolve_tenant_manifest", _fail)
    settings = make_settings(default_manifest="quick")
    common: dict[str, Any] = {"settings": settings, "tools": InMemoryToolProvider(), "tenant_id": "default"}

    with caplog.at_level("ERROR", logger="felix.a2a.server"):
        sent = await handle_rpc(
            **common,
            method="message/send",
            params={"taskId": "task-1", "message": {"parts": [{"type": "text", "text": "hi"}]}},
            rpc_id=1,
        )
    got = await handle_rpc(**common, method="tasks/get", params={"id": "task-1"}, rpc_id=2)

    for task in (sent["result"], got["result"]):
        assert task["status"]["state"] == "failed"
        assert task["status"]["message"] == A2A_TASK_FAILED
        assert leak not in json.dumps(task)
    logged = [r.getMessage() for r in caplog.records]
    assert any("RuntimeError" in m and "db-internal.corp" in m for m in logged), logged
    # A secret in the text is masked, and no traceback (which nothing masks) is written.
    assert not any(secret in m for m in logged), logged
    assert any("[REDACTED]" in m for m in logged), logged
    assert not any(r.exc_info for r in caplog.records)
