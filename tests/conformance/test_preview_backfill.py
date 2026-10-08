"""The preview backfill, on both backends: what it fills, what it leaves, and whose it touches.

`felix sessions backfill-previews` reads each thread's first user message back out of its log for
threads that predate `GET /chat/sessions` listing a preview. Every case starts from a thread as
an older release left it -- metadata with no `preview`, timestamps in the past -- plus a log.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

BACKENDS = ["memory", "postgres"]
TENANT = "conformance"
OTHER = "conformance-other"
OLD_S = 1_000  # A thread last touched long ago: epoch seconds on the column, ms in the metadata.

parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


def _thread(tenant: str = TENANT, suffix: str | None = None) -> str:
    return f"{tenant}:{suffix or uuid.uuid4().hex}"


async def _old_thread(settings: Any, thread: str, *, tenant: str = TENANT, **meta: Any) -> None:
    """A thread's metadata as a release before `preview` wrote it."""
    labels = {"session_name": None, "phase": "idle", "created_at": OLD_S * 1000, "updated_at": OLD_S * 1000}
    labels.update(meta)
    if settings.database_url.startswith("memory://"):
        from felix.session import thread_state

        thread_state._meta_by_thread[thread] = {**thread_state._default_meta(), **labels}
        return
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant) as db:
        db.add(ThreadState(tenant_id=tenant, thread_id=thread, labels_json=labels, updated_at=OLD_S))
        await db.commit()


async def _log(
    settings: Any, thread: str, *events: tuple[str, str | None, str | None], tenant: str = TENANT
) -> None:
    """Append ``(kind, role, content)`` events to the thread's session log, under its tenant."""
    from felix.db.session import rls_tenant
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    with rls_tenant(tenant):
        session = get_session_store(settings, tenant_id=tenant).open(thread)
        await session.append_batch([AppendableEvent(kind=k, role=r, content=c) for k, r, c in events])  # type: ignore[arg-type]


async def _meta(settings: Any, thread: str, tenant: str = TENANT) -> dict[str, Any]:
    from felix.session.thread_state import get_thread_meta

    return await get_thread_meta(settings=settings, tenant_id=tenant, thread_id=thread)


async def _column_updated_at(settings: Any, thread: str, tenant: str = TENANT) -> int | None:
    if settings.database_url.startswith("memory://"):
        return None
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant) as db:
        row = await db.get(ThreadState, (tenant, thread))
        assert row is not None
        return row.updated_at


async def _run(settings: Any, **kwargs: Any) -> dict[str, Any]:
    from felix.session.preview_backfill import backfill_previews

    return {r.tenant_id: r for r in await backfill_previews(settings, **kwargs)}


@parametrized
@pytest.mark.asyncio
async def test_a_thread_without_a_preview_gets_its_first_user_message(store_settings: Any) -> None:
    from felix.session.thread_state import masked_preview

    thread = _thread()
    await _old_thread(store_settings, thread)
    first = "  plan the\n\n garden  " + "and the orchard " * 20
    await _log(
        store_settings,
        thread,
        ("message", "user", first),
        ("message", "assistant", "sure"),
        ("message", "user", "and later this"),
    )

    reports = await _run(store_settings, tenant_id=TENANT)

    assert (reports[TENANT].scanned, reports[TENANT].filled) == (1, 1)
    meta = await _meta(store_settings, thread)
    # Exactly what a turn would have recorded for the same message.
    assert meta["preview"] == masked_preview(first)
    assert meta["preview"].startswith("plan the garden and the orchard")
    assert meta["preview"].endswith("…")


@parametrized
@pytest.mark.asyncio
async def test_the_backfill_moves_neither_timestamp_and_creates_no_thread(store_settings: Any) -> None:
    from felix.session.thread_state import backfill_preview, list_thread_metadata

    thread = _thread()
    await _old_thread(store_settings, thread, revision=3)
    await _log(store_settings, thread, ("message", "user", "hello"))

    await _run(store_settings, tenant_id=TENANT)

    meta = await _meta(store_settings, thread)
    assert (meta["preview"], meta["updated_at"], meta["created_at"]) == ("hello", OLD_S * 1000, OLD_S * 1000)
    assert meta["revision"] == 4, "the write is still counted"
    column = await _column_updated_at(store_settings, thread)
    assert column in (None, OLD_S), "retention's idle clock moved"

    # An id with a log and no metadata is not a thread the index lists, and stays that way.
    ghost = _thread()
    await _log(store_settings, ghost, ("message", "user", "orphan"))
    assert not await backfill_preview(
        settings=store_settings, tenant_id=TENANT, thread_id=ghost, text="orphan"
    )
    listed = {m["id"] for m in await list_thread_metadata(settings=store_settings, tenant_id=TENANT)}
    assert ghost not in listed


@parametrized
@pytest.mark.asyncio
async def test_a_thread_with_a_preview_keeps_it(store_settings: Any) -> None:
    thread = _thread()
    await _old_thread(store_settings, thread, preview="what the turn recorded")
    await _log(store_settings, thread, ("message", "user", "something else entirely"))

    reports = await _run(store_settings, tenant_id=TENANT)

    assert reports[TENANT].scanned == 0, "a thread with a preview was listed"
    assert (await _meta(store_settings, thread))["preview"] == "what the turn recorded"


@parametrized
@pytest.mark.asyncio
async def test_a_preview_a_turn_records_after_the_listing_wins(store_settings: Any) -> None:
    """The write checks again under the lock: listed as missing, filled by a turn, then reached."""
    from felix.session.thread_state import backfill_preview, note_first_message, threads_missing_preview

    thread = _thread()
    await _old_thread(store_settings, thread)
    assert await threads_missing_preview(settings=store_settings, tenant_id=TENANT) == [thread]
    await note_first_message(settings=store_settings, tenant_id=TENANT, thread_id=thread, text="the turn's")

    assert not await backfill_preview(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, text="the log's"
    )
    assert (await _meta(store_settings, thread))["preview"] == "the turn's"


@parametrized
@pytest.mark.asyncio
async def test_a_thread_with_no_user_text_stays_null(store_settings: Any) -> None:
    empty, blank, other_kinds = _thread(), _thread(), _thread()
    for thread in (empty, blank, other_kinds):
        await _old_thread(store_settings, thread)
    await _log(store_settings, blank, ("message", "user", " \n\t "), ("message", "assistant", "an answer"))
    await _log(
        store_settings,
        other_kinds,
        ("custom", "user", "a client entry, not the operator's message"),
        ("message", "system", "a system note"),
    )

    reports = await _run(store_settings, tenant_id=TENANT)

    assert (reports[TENANT].scanned, reports[TENANT].filled, reports[TENANT].no_text) == (3, 0, 3)
    for thread in (empty, blank, other_kinds):
        assert not (await _meta(store_settings, thread)).get("preview"), thread


@parametrized
@pytest.mark.asyncio
async def test_a_blank_first_user_turn_is_passed_over_for_the_next(store_settings: Any) -> None:
    """What a turn does with an image-only message: leaves the slot for the next one."""
    thread = _thread()
    await _old_thread(store_settings, thread)
    await _log(
        store_settings,
        thread,
        ("message", "user", ""),
        ("message", "assistant", "that is a picture of a cat"),
        ("message", "user", "what breed?"),
    )

    await _run(store_settings, tenant_id=TENANT)

    assert (await _meta(store_settings, thread))["preview"] == "what breed?"


@parametrized
@pytest.mark.asyncio
async def test_a_secret_is_masked_even_when_the_log_predates_its_masking(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log masks with the secrets known when it was written; the preview with today's."""
    import felix.secrets as secrets_mod

    secret = "sk-backfill-secret-41c9"
    thread = _thread()
    await _old_thread(store_settings, thread)
    monkeypatch.setattr(secrets_mod, "collected_secret_values", lambda *a, **k: [])
    await _log(store_settings, thread, ("message", "user", f"deploy with {secret} please"))
    monkeypatch.setattr(secrets_mod, "collected_secret_values", lambda *a, **k: [secret])

    await _run(store_settings, tenant_id=TENANT)

    preview = (await _meta(store_settings, thread))["preview"]
    assert preview == "deploy with [REDACTED] please"
    assert secret not in preview


@parametrized
@pytest.mark.asyncio
async def test_running_it_again_changes_nothing(store_settings: Any) -> None:
    filled, textless = _thread(), _thread()
    await _old_thread(store_settings, filled)
    await _old_thread(store_settings, textless)
    await _log(store_settings, filled, ("message", "user", "first"))

    first = await _run(store_settings, tenant_id=TENANT)
    meta = await _meta(store_settings, filled)
    second = await _run(store_settings, tenant_id=TENANT)

    assert (first[TENANT].filled, second[TENANT].filled) == (1, 0)
    assert second[TENANT].scanned == 1, "only the thread with no user text is looked at again"
    assert await _meta(store_settings, filled) == meta, "a re-run wrote to a filled thread"


@parametrized
@pytest.mark.asyncio
async def test_a_dry_run_counts_and_writes_nothing(store_settings: Any) -> None:
    thread = _thread()
    await _old_thread(store_settings, thread)
    await _log(store_settings, thread, ("message", "user", "hello"))

    reports = await _run(store_settings, tenant_id=TENANT, dry_run=True)

    assert reports[TENANT].filled == 1
    assert not (await _meta(store_settings, thread)).get("preview")


@parametrized
@pytest.mark.asyncio
async def test_pages_cover_every_thread(store_settings: Any) -> None:
    threads = [_thread(suffix=f"page-{n}") for n in range(7)]
    for n, thread in enumerate(threads):
        await _old_thread(store_settings, thread)
        if n != 3:
            await _log(store_settings, thread, ("message", "user", f"message {n}"))

    reports = await _run(store_settings, tenant_id=TENANT, batch_size=2)

    assert (reports[TENANT].scanned, reports[TENANT].filled, reports[TENANT].no_text) == (7, 6, 1)
    for n, thread in enumerate(threads):
        expected = None if n == 3 else f"message {n}"
        assert (await _meta(store_settings, thread)).get("preview") == expected


@parametrized
@pytest.mark.asyncio
async def test_each_tenant_is_filled_from_its_own_log_and_only_when_asked(store_settings: Any) -> None:
    mine, theirs = _thread(TENANT, "same-suffix"), _thread(OTHER, "same-suffix")
    await _old_thread(store_settings, mine)
    await _old_thread(store_settings, theirs, tenant=OTHER)
    await _log(store_settings, mine, ("message", "user", "ours"))
    await _log(store_settings, theirs, ("message", "user", "theirs"), tenant=OTHER)

    one = await _run(store_settings, tenant_id=TENANT)
    assert set(one) == {TENANT}
    assert (await _meta(store_settings, mine))["preview"] == "ours"
    assert not (await _meta(store_settings, theirs, OTHER)).get("preview"), "another tenant was written"

    every = await _run(store_settings)
    assert {TENANT, OTHER} <= set(every)
    assert every[OTHER].filled == 1
    assert (await _meta(store_settings, theirs, OTHER))["preview"] == "theirs"
    assert (await _meta(store_settings, mine))["preview"] == "ours"


@pytest.mark.asyncio
async def test_under_an_enforced_policy_every_tenant_is_still_reached(rls_settings: Any) -> None:
    """A role RLS applies to: the tenant list is read across tenants, each tenant's rows under it."""
    mine, theirs = _thread(TENANT), _thread(OTHER)
    await _old_thread(rls_settings, mine)
    await _old_thread(rls_settings, theirs, tenant=OTHER)
    await _log(rls_settings, mine, ("message", "user", "ours"))
    await _log(rls_settings, theirs, ("message", "user", "theirs"), tenant=OTHER)

    reports = await _run(rls_settings)

    assert {t: r.filled for t, r in reports.items()} == {TENANT: 1, OTHER: 1}
    assert (await _meta(rls_settings, mine))["preview"] == "ours"
    assert (await _meta(rls_settings, theirs, OTHER))["preview"] == "theirs"
