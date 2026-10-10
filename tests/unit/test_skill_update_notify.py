"""`skill.update_available`: queued when a recorded check finds a new upstream digest, sent by the
worker's sweep to the endpoints bound to the skill's tenant.

GitHub is `tests/support/skill_import_fake.py` at the transport and the stores are the `memory://` twins.
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
from felix.durability import webhooks
from felix.durability.webhooks import parse_tenant_endpoint_bindings
from felix.skills import importer, library, update_notify, upstream, upstream_store
from felix.skills.library_keys import ORG_OWNER
from felix.skills.update_notify import deliver_due_notifications, event_id
from felix.skills.upstream_store import get_upstream_store
from felix.storage import MemoryObjectStore

from tests.support.skill_import_fake import FakeRepos, skill_md
from tests.support.webhook_receiver import receiver

REPO = "acme/skills"
NAME = "invoice-triage"
SOURCE = f"github:{REPO}/skills/{NAME}"
T0 = 1_750_000_000_000
DAY = importer.DAY_MS
SECRET = "a-shared-secret-long-enough"
# Text that is only ever in the skill's files: a payload carrying any of it carried file content.
BODY_MARKER = "Route every invoice to the ledger team"
QUEUES = b"# Queues\n\nfinance, ops\n"
UNREACHABLE = "https://x.test/h"


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


# The same stores, with no binding: what an import made on a process configured without one does.
UNBOUND = Settings(database_url="memory://skill-update-notify", object_store="memory")


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


async def _import(
    settings: Settings, store: Any, gh: FakeRepos, *, tenant: str = "acme", at: int = T0
) -> Any:
    async with gh.client() as http:
        return await importer.import_skill(
            settings, tenant, source=SOURCE, by="ops", deps=_deps(http, store, at)
        )


async def _update(settings: Settings, store: Any, gh: FakeRepos, *, at: int) -> Any:
    async with gh.client() as http:
        return await upstream.update_skill(settings, "acme", NAME, by="ops", deps=_deps(http, store, at))


async def _check(
    settings: Settings, store: Any, gh: FakeRepos, *, tenant: str = "acme", at: int = T0 + 1, **kw: Any
) -> dict[str, Any]:
    async with gh.client() as http:
        return await upstream.check_upstream(settings, tenant, NAME, deps=_deps(http, store, at), **kw)


async def _listing(settings: Settings, store: Any, gh: FakeRepos, *, at: int) -> dict[str, Any]:
    async with gh.client() as http:
        return await upstream.outdated(settings, "acme", deps=_deps(http, store, at))


def _row(tenant: str = "acme") -> dict[str, Any]:
    return upstream_store._memory._rows[(tenant, NAME)]


def _move(gh: FakeRepos, queues: bytes, *, ref: str = "main") -> str:
    return gh.push(REPO, _tree(_files(queues)), ref=ref)


def _verified(request: dict[str, Any]) -> bool:
    headers, body = request["headers"], request["body"]
    mac = hmac.new(
        SECRET.encode(),
        f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body,
        hashlib.sha256,
    )
    return headers["webhook-signature"] == "v1," + base64.b64encode(mac.digest()).decode()


# -- the setting ---------------------------------------------------------------------------------


def test_bindings_parse_per_tenant_and_refuse_a_wildcard() -> None:
    assert parse_tenant_endpoint_bindings("acme=ops, acme=ci,beta=beta-hook,acme=ops") == {
        "acme": ("ops", "ci"),
        "beta": ("beta-hook",),
    }
    assert parse_tenant_endpoint_bindings("") == {}
    for bad in ("acme", "=ops", "acme=", "*=ops", "acme=*"):
        with pytest.raises(ValueError):
            parse_tenant_endpoint_bindings(bad)


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
    endpoints = {"ops": (UNREACHABLE, ["acme"]), "beta-hook": ("https://y.test/h", ["beta"])}
    _settings(endpoints, "acme=ops,beta=beta-hook").validate_runtime()  # the valid one boots
    with pytest.raises(RuntimeError, match=rf"FELIX_SKILL_UPDATE_WEBHOOKS: .*{fragment.replace('*', r'\*')}"):
        _settings(endpoints, bindings).validate_runtime()


# -- detection -----------------------------------------------------------------------------------


async def test_a_new_digest_is_queued_once_and_an_unchanged_one_never(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        await _check(settings, store, gh)
        assert _row()["notify_status"] is None, "an unchanged upstream is not an update"

        _move(gh, b"moved\n")
        first = await _check(settings, store, gh, at=T0 + 2)
        assert (_row()["notify_status"], _row()["notified_tree_hash"]) == (
            "pending",
            first["upstream"]["tree_hash"],
        )
        assert await deliver_due_notifications(settings) == 1
        assert _row()["notify_status"] == "delivered"

        # The same digest found again, by a check and by a refreshed listing: not announced again.
        await _check(settings, store, gh, at=T0 + 3)
        await _listing(settings, store, gh, at=T0 + 4)
        assert await deliver_due_notifications(settings) == 0
    assert len(seen) == 1, "one event per digest"
    assert _row()["notify_status"] == "delivered"


async def test_a_refreshed_listing_queues_a_new_digest(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    _move(gh, b"moved\n")

    listing = await _listing(settings, store, gh, at=T0 + 1)

    assert listing["items"][0]["update_available"] is True
    assert _row()["notify_status"] == "pending"
    event = _row()["notify_state"]["event"]
    assert event["checked_at"] == T0 + 1
    assert "changed_files" not in event, "the listing computes no diff, and spends nothing to count one"


async def test_changed_files_is_left_out_when_the_check_diffed_against_another_version(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """With 0.1.0 live and an update drafted as 0.1.1, a check diffs against the live version;
    the event calls 0.1.1 current, so the count would describe the wrong pair."""
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    await library.publish(settings, "acme", NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    _move(gh, b"first move\n")
    await _update(settings, store, gh, at=T0 + 1)
    _move(gh, b"second move\n")

    checked = await _check(settings, store, gh, at=T0 + 2)

    assert checked["current"]["version"] == "0.1.1" and checked["diff"]["compared_with"] == "0.1.0"
    event = _row()["notify_state"]["event"]
    assert event["current"]["version"] == "0.1.1"
    assert "changed_files" not in event


async def test_a_what_if_check_of_another_ref_queues_nothing(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    _move(gh, b"side\n", ref="next")

    checked = await _check(settings, store, gh, ref="next")
    assert checked["update_available"] is True
    assert _row()["notify_status"] is None, "a ?ref= check is never recorded"

    # The control: the same files on the stored ref, checked the same way, are queued.
    _move(gh, b"side\n")
    await _check(settings, store, gh, at=T0 + 2)
    assert _row()["notify_status"] == "pending"


async def test_a_tenant_with_no_binding_queues_nothing(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops")
    await _import(settings, store, gh, tenant="initech")
    await _import(settings, store, gh, tenant="acme")
    _move(gh, b"moved\n")

    await _check(settings, store, gh, tenant="initech")
    await _check(settings, store, gh, tenant="acme")

    assert _row("initech")["notify_status"] is None
    assert _row("acme")["notify_status"] == "pending", "the control: a bound tenant's same check queues"


async def test_eligible_at_counts_from_the_first_sighting_not_the_check(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """A what-if check first sees the files at T0+1 and records nothing; the stored-ref check at
    T0+5 queues them. The cooldown runs from the sighting."""
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops", skill_import_min_age_days=7)
    await _import(UNBOUND, store, gh, at=T0 - 30 * DAY)
    _move(gh, b"moved\n", ref="next")
    _move(gh, b"moved\n")

    await _check(settings, store, gh, ref="next", at=T0 + 1)
    await _check(settings, store, gh, at=T0 + 5)

    event = _row()["notify_state"]["event"]
    assert (event["checked_at"], event["upstream"]["first_seen_at"]) == (T0 + 5, T0 + 1)
    assert event["eligible_at"] == T0 + 1 + 7 * DAY


async def test_a_failing_notification_never_fails_a_check_a_listing_or_an_import(
    store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings({"ops": (UNREACHABLE, ["acme"])}, "acme=ops")
    await _import(settings, store, gh)
    notify_store = get_upstream_store(settings)

    async def down(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the notification store is down")

    monkeypatch.setattr(notify_store, "enqueue_notification", down)
    monkeypatch.setattr(notify_store, "cancel_notification", down)
    commit = _move(gh, b"moved\n")

    checked = await _check(settings, store, gh, at=T0 + 1)
    listing = await _listing(settings, store, gh, at=T0 + 2)
    result, _ = await _update(settings, store, gh, at=T0 + 3)

    assert checked["upstream"]["commit"] == commit and checked["update_available"] is True
    assert listing["items"][0]["upstream_commit"] == commit
    assert result.version["origin_commit"] == commit and result.unchanged is False
    assert _row()["checked_at"] == T0 + 3, "every check was still recorded"


# -- delivery ------------------------------------------------------------------------------------


async def test_the_event_is_metadata_only_signed_and_the_same_bytes_on_every_retry(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    async with receiver([500, 200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        imported = (await upstream.recorded_state(settings, "acme", NAME, now=T0)) or {}
        commit = _move(gh, b"moved\n")
        checked = await _check(settings, store, gh, at=T0 + 1)

        assert await deliver_due_notifications(settings) == 1
        # Found again between the attempts: the queued event is not rebuilt from the new check.
        await _check(settings, store, gh, at=T0 + 5)
        _row()["notify_due_at"] = 0  # the backoff lapses
        assert await deliver_due_notifications(settings) == 1

    assert len(seen) == 2
    first, second = seen
    tree_hash = checked["upstream"]["tree_hash"]
    assert (
        first["headers"]["webhook-id"]
        == second["headers"]["webhook-id"]
        == event_id("acme", NAME, tree_hash, 1)
    )
    assert first["body"] == second["body"], "every retry sends the bytes queued at detection"
    assert all(_verified(r) for r in seen)

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
    assert _row()["notify_status"] == "delivered"


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


@pytest.mark.parametrize(
    ("then", "ops_tenants", "bindings"),
    [
        ("rebound to another tenant", ["acme", "beta"], "beta=ops"),
        ("fenced to another tenant", ["beta"], "acme=ops"),
    ],
)
async def test_an_endpoint_taken_from_the_tenant_before_delivery_is_dead_unsent(
    store: MemoryObjectStore, gh: FakeRepos, then: str, ops_tenants: list[str], bindings: str
) -> None:
    async with receiver([200]) as (url, seen):
        queued_under = _settings({"ops": (url, ["acme", "beta"])}, "acme=ops")
        await _import(queued_under, store, gh)
        _move(gh, b"moved\n")
        await _check(queued_under, store, gh)
        assert _row()["notify_status"] == "pending"

        await deliver_due_notifications(_settings({"ops": (url, ops_tenants)}, bindings))

    assert seen == [], f"sent to an endpoint {then}"
    ops = _row()["notify_state"]["endpoints"]["ops"]
    assert (_row()["notify_status"], ops["status"]) == ("dead", "dead")
    assert "no longer registered" in ops["last_error"]


async def test_a_failing_endpoint_is_retried_then_dead(store: MemoryObjectStore, gh: FakeRepos) -> None:
    async with receiver([500]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops", webhook_max_attempts=2)
        await _import(settings, store, gh)
        _move(gh, b"moved\n")
        await _check(settings, store, gh)

        await deliver_due_notifications(settings)
        assert (_row()["notify_status"], _row()["notify_attempts"]) == ("pending", 1)
        assert _row()["notify_state"]["endpoints"]["ops"]["last_error"] == "HTTP 500"
        assert await deliver_due_notifications(settings) == 0, "backed off, not retried on the next tick"

        _row()["notify_due_at"] = 0
        await deliver_due_notifications(settings)
    assert len(seen) == 2
    assert (_row()["notify_status"], _row()["notify_state"]["endpoints"]["ops"]["status"]) == ("dead", "dead")


async def test_several_endpoints_share_one_id_and_each_is_counted_under_its_kind(
    store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted: list[dict[str, str]] = []
    monkeypatch.setattr(webhooks, "record_counter", lambda _name, labels: counted.append(dict(labels)))
    async with receiver([200]) as (ok_url, ok_seen), receiver([500]) as (bad_url, bad_seen):
        settings = _settings(
            {"ok": (ok_url, ["acme"]), "bad": (bad_url, ["acme"])}, "acme=ok,acme=bad", webhook_max_attempts=1
        )
        await _import(settings, store, gh)
        _move(gh, b"moved\n")
        await _check(settings, store, gh)
        await deliver_due_notifications(settings)

        # A finished durable run to the same endpoint, through the run sweep.
        from felix.durability import fibers as F

        run = await F.create_fiber(
            settings, "acme", kind="durable_chat", state={"steps": []}, webhooks=["ok"]
        )
        F._memory_fibers[("acme", run["id"])]["status"] = "completed"
        await webhooks.deliver_due_webhooks(settings)

    [ok_request, run_request], [bad_request] = ok_seen, bad_seen
    assert ok_request["headers"]["webhook-id"] == bad_request["headers"]["webhook-id"]
    endpoints = _row()["notify_state"]["endpoints"]
    assert (_row()["notify_status"], endpoints["ok"]["status"], endpoints["bad"]["status"]) == (
        "dead",
        "delivered",
        "dead",
    )
    assert json.loads(run_request["body"])["type"] == "run.completed"
    assert sorted((c["kind"], c["endpoint"], c["outcome"]) for c in counted) == [
        ("run", "ok", "delivered"),
        ("skill_update", "bad", "dead"),
        ("skill_update", "ok", "delivered"),
    ]


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

        async def crash(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("the worker died")

        with monkeypatch.context() as m:
            m.setattr(get_upstream_store(settings), "save_notification", crash)
            assert await deliver_due_notifications(settings) == 1
        assert await deliver_due_notifications(settings) == 0, "still claimed"

        later = update_notify._now_ms() + update_notify.NOTIFY_CLAIM_MS + 1
        monkeypatch.setattr(update_notify, "_now_ms", lambda: later)
        assert await deliver_due_notifications(settings) == 1
    assert len(seen) == 2
    assert _row()["notify_status"] == "delivered"


# -- only the newest, and only news --------------------------------------------------------------


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
    assert request["headers"]["webhook-id"] == event_id("acme", NAME, newer["upstream"]["tree_hash"], 2)


async def test_a_digest_queued_during_a_delivery_is_kept_and_sent_next(
    store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The origin moves, and a check queues the newer digest, while the older one is being sent:
    the older delivery's outcome must not overwrite it."""
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        _move(gh, b"first move\n")
        await _check(settings, store, gh, at=T0 + 1)
        attempt = webhooks.WebhookSender.attempt

        async def racing(self: Any, *args: Any, **kw: Any) -> Any:
            sent = await attempt(self, *args, **kw)
            _move(gh, b"second move\n")
            await _check(settings, store, gh, at=T0 + 2)
            return sent

        with monkeypatch.context() as m:
            m.setattr(webhooks.WebhookSender, "attempt", racing)
            await deliver_due_notifications(settings)
        newer = _row()["notified_tree_hash"]
        assert (_row()["notify_status"], _row()["notify_generation"]) == ("pending", 2)

        assert await deliver_due_notifications(settings) == 1
    assert [json.loads(r["body"])["upstream"]["tree_hash"] for r in seen][1] == newer
    assert seen[1]["headers"]["webhook-id"] == event_id("acme", NAME, newer, 2)
    assert _row()["notify_status"] == "delivered"


async def test_a_digest_that_comes_back_is_a_new_event(store: MemoryObjectStore, gh: FakeRepos) -> None:
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        for at, queues in ((T0 + 1, b"A\n"), (T0 + 2, b"B\n"), (T0 + 3, b"A\n")):
            _move(gh, queues)
            await _check(settings, store, gh, at=at)
            assert await deliver_due_notifications(settings) == 1

    ids = [r["headers"]["webhook-id"] for r in seen]
    hashes = [json.loads(r["body"])["upstream"]["tree_hash"] for r in seen]
    assert hashes[0] == hashes[2] != hashes[1]
    assert len(set(ids)) == 3, "A, B, then A again is three events, not a duplicate of the first"


@pytest.mark.parametrize("caught", ["by the import", "at delivery"])
async def test_an_update_someone_imported_is_never_sent(
    store: MemoryObjectStore, gh: FakeRepos, caught: str
) -> None:
    """A is queued, then imported before the sweep runs. `at delivery`: the import ran on a
    process with no binding, so only the delivery's own check is left to catch it."""
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        _move(gh, b"A\n")
        await _check(settings, store, gh, at=T0 + 1)

        await _update(settings if caught == "by the import" else UNBOUND, store, gh, at=T0 + 2)
        assert _row()["notify_status"] == ("superseded" if caught == "by the import" else "pending")
        await deliver_due_notifications(settings)

    assert seen == [], f"an update already imported was announced ({caught})"
    assert _row()["notify_status"] == "superseded"


@pytest.mark.parametrize("caught", ["by the import", "at delivery"])
async def test_an_update_the_origin_moved_past_is_never_sent(
    store: MemoryObjectStore, gh: FakeRepos, caught: str
) -> None:
    """A is queued; the origin moves to B and an update imports B before the sweep runs."""
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": (url, ["acme"])}, "acme=ops")
        await _import(settings, store, gh)
        _move(gh, b"A\n")
        await _check(settings, store, gh, at=T0 + 1)
        _move(gh, b"B\n")

        await _update(settings if caught == "by the import" else UNBOUND, store, gh, at=T0 + 2)
        assert _row()["notify_status"] == ("superseded" if caught == "by the import" else "pending")
        await deliver_due_notifications(settings)

    assert seen == [], f"an update the origin moved past was announced ({caught})"
    assert _row()["notify_status"] == "superseded"


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
        assert _row()["notify_status"] == "pending"

        assert await deliver_due_notifications(settings) == 1
    [request] = seen
    event = json.loads(request["body"])
    assert (event["upstream"]["commit"], event["checked_at"]) == (commit, T0 + 3_600_000)
    assert "changed_files" not in event, "the sweep computes no diff, and spends nothing to count one"
