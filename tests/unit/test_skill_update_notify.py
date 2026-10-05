"""`skill.update_available`: queued when a recorded check finds a new upstream digest, sent by the
worker's sweep to the endpoints bound to the skill's tenant.

GitHub is `tests/skill_import_fake.py` at the transport and the stores are the `memory://` twins.
The receiving end is a real HTTP server on loopback (`test_completion_webhooks.receiver`), reached
through the real egress guard, because what matters about a webhook is what arrives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import importer, update_notify, upstream
from felix.skills.update_notify import deliver_due_notifications, event_id, parse_skill_update_webhooks
from felix.skills.upstream_store import get_upstream_store
from felix.storage import MemoryObjectStore

from tests.skill_import_fake import FakeRepos, skill_md
from tests.unit.test_completion_webhooks import receiver

REPO = "acme/skills"
NAME = "invoice-triage"
SOURCE = f"github:{REPO}/skills/{NAME}"
T0 = 1_750_000_000_000
SECRET = "a-shared-secret-long-enough"
# Text that is only ever in the skill's files: a payload carrying any of it carried file content.
BODY_MARKER = "Route every invoice to the ledger team"
QUEUES = b"# Queues\n\nfinance, ops\n"


def _tree(files: dict[str, bytes]) -> dict[str, bytes]:
    return {f"skills/{NAME}/{path}": data for path, data in files.items()}


def _files(queues: bytes = QUEUES) -> dict[str, bytes]:
    return {"SKILL.md": skill_md(NAME, body=BODY_MARKER), "references/queues.md": queues}


def _settings(endpoints: dict[str, tuple[str, list[str]]], bindings: str, **kw: Any) -> Settings:
    """``endpoints``: id -> (url, the tenants it opens to)."""
    registry = {
        name: {"url": url, "secret": SECRET, "tenants": tenants} for name, (url, tenants) in endpoints.items()
    }
    return Settings(
        database_url="memory://skill-update-notify",
        object_store="memory",
        environment="development",
        allow_insecure=True,
        webhook_endpoints=json.dumps(registry),
        skill_update_webhooks=bindings,
        **kw,
    )


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeRepos:
    fake = FakeRepos()
    fake.serve(monkeypatch)
    fake.push(REPO, _tree(_files()))
    return fake


def _deps(http: Any, store: Any, at: int) -> importer.ImportDeps:
    return importer.ImportDeps(http=http, object_store=store, charge=importer.uncharged(), clock=lambda: at)


async def _import(settings: Settings, store: Any, gh: FakeRepos, *, tenant: str = "acme") -> None:
    async with gh.client() as http:
        await importer.import_skill(settings, tenant, source=SOURCE, by="ops", deps=_deps(http, store, T0))


async def _check(
    settings: Settings, store: Any, gh: FakeRepos, *, tenant: str = "acme", at: int = T0 + 1, **kw: Any
) -> dict[str, Any]:
    async with gh.client() as http:
        return await upstream.check_upstream(settings, tenant, NAME, deps=_deps(http, store, at), **kw)


async def _notify_row(tenant: str = "acme") -> dict[str, Any]:
    from felix.skills import upstream_store

    return dict(upstream_store._memory._rows[(tenant, NAME)])


def _move(gh: FakeRepos, queues: bytes, *, ref: str = "main") -> str:
    return gh.push(REPO, _tree(_files(queues)), ref=ref)


# -- the setting ---------------------------------------------------------------------------------


def test_bindings_parse_per_tenant_and_refuse_a_wildcard() -> None:
    assert parse_skill_update_webhooks("acme=ops, acme=ci,beta=beta-hook,acme=ops") == {
        "acme": ("ops", "ci"),
        "beta": ("beta-hook",),
    }
    assert parse_skill_update_webhooks("") == {}
    for bad in ("acme", "=ops", "acme=", "*=ops", "acme=*"):
        with pytest.raises(ValueError):
            parse_skill_update_webhooks(bad)


@pytest.mark.parametrize(
    ("bindings", "fragment"),
    [
        ("acme=nowhere", "acme=nowhere"),
        # Registered, but fenced to another tenant: binding it would send acme's events there.
        ("acme=beta-hook", "acme=beta-hook"),
        ("*=ops", "wildcard"),
    ],
)
def test_boot_is_refused_on_a_binding_to_an_endpoint_not_open_to_its_tenant(
    bindings: str, fragment: str
) -> None:
    endpoints = {"ops": ("https://x.test/h", ["acme"]), "beta-hook": ("https://y.test/h", ["beta"])}
    _settings(endpoints, "acme=ops,beta=beta-hook").validate_runtime()  # the valid one boots
    with pytest.raises(RuntimeError, match=rf"FELIX_SKILL_UPDATE_WEBHOOKS: .*{fragment.replace('*', r'\*')}"):
        _settings(endpoints, bindings).validate_runtime()


# -- detection -----------------------------------------------------------------------------------


async def test_a_new_digest_is_queued_once_and_an_unchanged_one_never(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    settings = _settings({"ops": ("https://x.test/h", ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    await _check(settings, store, gh)
    assert (await _notify_row())["notify_status"] is None, "an unchanged upstream is not an update"

    _move(gh, b"moved\n")
    first = await _check(settings, store, gh, at=T0 + 2)
    row = await _notify_row()
    assert row["notify_status"] == "pending"
    assert row["notified_tree_hash"] == first["upstream"]["tree_hash"]

    # Delivered (as far as the row knows); the same digest found again is not announced again.
    from felix.skills import upstream_store

    upstream_store._memory._rows[("acme", NAME)]["notify_status"] = "delivered"
    await _check(settings, store, gh, at=T0 + 3)
    async with gh.client() as http:
        await upstream.outdated(settings, "acme", deps=_deps(http, store, T0 + 4))
    assert (await _notify_row())["notify_status"] == "delivered", "one event per digest"


async def test_a_what_if_check_of_another_ref_queues_nothing(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _settings({"ops": ("https://x.test/h", ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    _move(gh, b"side\n", ref="next")

    checked = await _check(settings, store, gh, ref="next")

    assert checked["update_available"] is True
    assert (await _notify_row())["notify_status"] is None, "a ?ref= check is never recorded"


async def test_a_tenant_with_no_binding_queues_nothing(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _settings({"ops": ("https://x.test/h", ["acme"])}, "acme=ops")
    await _import(settings, store, gh, tenant="initech")
    _move(gh, b"moved\n")

    await _check(settings, store, gh, tenant="initech")

    assert (await _notify_row("initech"))["notify_status"] is None


# -- delivery ------------------------------------------------------------------------------------


async def test_the_event_is_metadata_only_signed_and_stable_across_retries(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    async with receiver([500, 200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        imported = (await upstream.recorded_state(settings, "acme", NAME, now=T0)) or {}
        commit = _move(gh, b"moved\n")
        checked = await _check(settings, store, gh)

        assert await deliver_due_notifications(settings) == 1
        from felix.skills import upstream_store

        upstream_store._memory._rows[("acme", NAME)]["notify_due_at"] = 0  # the backoff lapses
        assert await deliver_due_notifications(settings) == 1

    assert len(seen) == 2
    first, second = seen
    tree_hash = checked["upstream"]["tree_hash"]
    assert (
        first["headers"]["webhook-id"] == second["headers"]["webhook-id"] == event_id("acme", NAME, tree_hash)
    )
    assert first["body"] == second["body"], "every retry sends the bytes queued at detection"
    for request in seen:
        headers, body = request["headers"], request["body"]
        mac = hmac.new(
            SECRET.encode(),
            f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body,
            hashlib.sha256,
        )
        assert headers["webhook-signature"] == "v1," + base64.b64encode(mac.digest()).decode()

    event = json.loads(second["body"])
    assert event == {
        "type": "skill.update_available",
        "tenant_id": "acme",
        "skill": NAME,
        "source": SOURCE,
        "ref": "main",
        "current": {
            "version": "0.1.0",
            "commit": checked["current"]["commit"],
            "tree_hash": imported["upstream_tree_hash"],
        },
        "upstream": {
            "commit": commit,
            "tree_hash": tree_hash,
            "committed_at": checked["upstream"]["committed_at"],
            "first_seen_at": T0 + 1,
        },
        "eligible_at": T0 + 1,
        "checked_at": T0 + 1,
        # Known from the check's own diff, against the version the event calls current.
        "changed_files": 1,
    }
    for text in (BODY_MARKER, "moved", "finance"):
        assert text.encode() not in second["body"], f"file content {text!r} left the deployment"
    assert (await _notify_row())["notify_status"] == "delivered"


async def test_a_tenants_update_reaches_only_its_own_endpoints(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """Both endpoints open to both tenants, as a shared receiver's would be: the binding alone
    decides where an event goes, not the endpoint's own `tenants` fence."""
    both = ["acme", "beta"]
    async with receiver([200]) as (acme_url, acme_seen), receiver([200]) as (beta_url, beta_seen):
        settings = _settings(
            {"ops": (acme_url, both), "beta-hook": (beta_url, both)}, "acme=ops,beta=beta-hook"
        )
        await _import(settings, store, gh, tenant="acme")
        await _import(settings, store, gh, tenant="beta")
        _move(gh, b"moved\n")

        await _check(settings, store, gh, tenant="beta")
        await deliver_due_notifications(settings)

    assert acme_seen == [], "beta's update reached acme's endpoint"
    [request] = beta_seen
    assert json.loads(request["body"])["tenant_id"] == "beta"


async def test_a_failing_endpoint_is_retried_then_dead(store: MemoryObjectStore, gh: FakeRepos) -> None:
    from felix.skills import upstream_store

    async with receiver([500]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops", webhook_max_attempts=2)
        await _import(settings, store, gh)
        _move(gh, b"moved\n")
        await _check(settings, store, gh)

        await deliver_due_notifications(settings)
        row = await _notify_row()
        assert (row["notify_status"], row["notify_attempts"]) == ("pending", 1)
        assert row["notify_state"]["endpoints"]["ops"]["last_error"] == "HTTP 500"
        assert await deliver_due_notifications(settings) == 0, "backed off, not retried on the next tick"

        upstream_store._memory._rows[("acme", NAME)]["notify_due_at"] = 0
        await deliver_due_notifications(settings)
    assert len(seen) == 2
    row = await _notify_row()
    assert (row["notify_status"], row["notify_state"]["endpoints"]["ops"]["status"]) == ("dead", "dead")


async def test_a_claim_held_by_a_crashed_sweep_lapses(
    store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first sweep posts and dies before recording it: its row stays claimed, so the next
    sweep leaves it alone until the claim lapses, then sends it."""
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        _move(gh, b"moved\n")
        await _check(settings, store, gh)

        notify_store = get_upstream_store(settings)

        async def crash(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("the worker died")

        monkeypatch.setattr(notify_store, "save_notification", crash)
        assert await deliver_due_notifications(settings) == 1
        monkeypatch.undo()
        assert await deliver_due_notifications(settings) == 0, "still claimed"

        later = update_notify._now_ms() + update_notify.NOTIFY_CLAIM_MS + 1
        monkeypatch.setattr(update_notify, "_now_ms", lambda: later)
        assert await deliver_due_notifications(settings) == 1
    assert len(seen) == 2
    assert (await _notify_row())["notify_status"] == "delivered"


async def test_a_newer_digest_replaces_an_undelivered_older_one(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        _move(gh, b"first move\n")
        older = await _check(settings, store, gh, at=T0 + 1)
        _move(gh, b"second move\n")
        newer = await _check(settings, store, gh, at=T0 + 2)
        assert older["upstream"]["tree_hash"] != newer["upstream"]["tree_hash"]

        await deliver_due_notifications(settings)
        assert await deliver_due_notifications(settings) == 0

    [request] = seen
    assert json.loads(request["body"])["upstream"]["tree_hash"] == newer["upstream"]["tree_hash"]
    assert request["headers"]["webhook-id"] == event_id("acme", NAME, newer["upstream"]["tree_hash"])


async def test_the_sweep_queues_and_only_the_delivery_sweep_sends(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """End to end on the worker's two sweeps: the check sweep finds the change and makes no call
    to the endpoint; the delivery sweep then sends it."""
    from felix.security.rate_limit import InMemoryRateLimiter

    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops", skill_import_check_hours=1)
        await _import(settings, store, gh)
        commit = _move(gh, b"moved\n")
        async with gh.client() as http:
            deps = importer.ImportDeps(http=http, clock=lambda: T0 + 3_600_000, charge=importer.uncharged())
            counts = await upstream.run_upstream_checks(settings, limiter=InMemoryRateLimiter(), deps=deps)
        assert counts["updates"] == 1
        assert seen == [], "the check made an HTTP call to the endpoint"
        assert (await _notify_row())["notify_status"] == "pending"

        assert await deliver_due_notifications(settings) == 1
    [request] = seen
    event = json.loads(request["body"])
    assert (event["upstream"]["commit"], event["checked_at"]) == (commit, T0 + 3_600_000)
    assert "changed_files" not in event, "the sweep computes no diff, and spends nothing to count one"
