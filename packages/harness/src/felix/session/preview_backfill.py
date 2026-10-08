"""Record a `preview` for the threads written before `GET /chat/sessions` listed one.

A turn records its thread's first user message as the preview (`thread_state.note_first_message`),
so every thread that existed before that lists as `preview: null` for good -- on a long-lived
deployment, nearly all of them. This reads each such thread's first user message back out of its
session log and records it through the same mask-and-cut rule (`thread_state.masked_preview`).

An operator runs it once after upgrading (`felix sessions backfill-previews`). It is not a
migration because the mask is the deployment's: `secrets.redact_text` masks the secret values
this process can see, which an Alembic revision has no business loading, and a migration holds
one transaction for the whole pass. Here every write is its own short transaction on one row.

The rules, each pinned by `tests/conformance/test_preview_backfill.py`:

- **Missing only.** A thread with a preview is never listed, and the write checks again under
  the row lock, so a turn that records one meanwhile keeps its own. Re-running is a no-op for
  every thread it filled.
- **The text a turn would have recorded.** The first `message` event with role `user` and any
  non-blank content, in log order -- what `ReactAgent._note_preview` records, since a turn's
  incoming user messages are appended before it notes one and a turn whose user messages are
  all blank (an image alone) leaves the slot for the next. `custom` entries are not the
  operator's message and are skipped. A thread with no such event is left null.
- **Tenant by tenant.** Each tenant's threads are listed and read under that tenant
  (`rls_tenant`, `tenant_session`); only the list of tenants is read across them.
- **Batched.** A page of thread ids at a time, keyset-paged, and a log read a page of message
  events at a time until the first user message -- never a whole log, never every log.
- **Nothing else moves.** Not `updated_at` (a client's sort order, and retention's idle clock),
  and no thread is created.

Threads whose log lives in a plugin checkpointer (`register_checkpointer`) rather than the
session store are read as empty and left null.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from felix.config import Settings
from felix.session.types import GetEventsOpts, Session

logger = logging.getLogger("felix.session.preview_backfill")

DEFAULT_BATCH_SIZE = 200
# Message events read per page of one log. The first user message is almost always the first
# message event, so one page is the usual cost; a thread opening with a run of blank or
# image-only user turns pages on.
_LOG_PAGE = 20


@dataclass(slots=True)
class PreviewBackfillReport:
    """What one tenant's pass did. ``filled`` is "would fill" under a dry run."""

    tenant_id: str
    scanned: int = 0
    filled: int = 0
    no_text: int = 0
    # Gained a preview (or lost its row) between being listed and being written.
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


async def first_user_text(session: Session) -> str | None:
    """The thread's first user message with any non-blank content, read a page at a time."""
    cursor = 0
    while True:
        page = await session.get_events(GetEventsOpts(from_seq=cursor, kinds=["message"], limit=_LOG_PAGE))
        for event in page:
            if event.role == "user" and event.content and event.content.strip():
                return event.content
        if len(page) < _LOG_PAGE:
            return None
        cursor = page[-1].seq + 1


async def backfill_tenant_previews(
    settings: Settings,
    tenant_id: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> PreviewBackfillReport:
    """Fill the preview of every thread of ``tenant_id`` that has none and has user text."""
    from felix.db.session import rls_tenant
    from felix.session.store import get_session_store
    from felix.session.thread_state import backfill_preview, masked_preview, threads_missing_preview

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    report = PreviewBackfillReport(tenant_id=tenant_id)
    store = get_session_store(settings, tenant_id=tenant_id)
    after: str | None = None
    with rls_tenant(tenant_id):
        while True:
            page = await threads_missing_preview(
                settings=settings, tenant_id=tenant_id, after=after, limit=batch_size
            )
            for thread_id in page:
                report.scanned += 1
                try:
                    text = await first_user_text(store.open(thread_id))
                    if masked_preview(text) is None:
                        report.no_text += 1
                    elif dry_run or await backfill_preview(
                        settings=settings, tenant_id=tenant_id, thread_id=thread_id, text=text
                    ):
                        report.filled += 1
                    else:
                        report.skipped += 1
                except Exception:
                    # One unreadable thread is not a reason to stop the rest; the caller exits
                    # non-zero and a re-run retries exactly the threads still missing one.
                    logger.warning("preview backfill failed for thread=%s", thread_id, exc_info=True)
                    report.failed.append(thread_id)
            if len(page) < batch_size:
                return report
            after = page[-1]


async def backfill_previews(
    settings: Settings,
    *,
    tenant_id: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> list[PreviewBackfillReport]:
    """`backfill_tenant_previews` for ``tenant_id``, or for every tenant holding threads."""
    from felix.session.thread_state import list_thread_tenants

    tenants = [tenant_id] if tenant_id else await list_thread_tenants(settings=settings)
    return [
        await backfill_tenant_previews(settings, t, batch_size=batch_size, dry_run=dry_run) for t in tenants
    ]


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "PreviewBackfillReport",
    "backfill_previews",
    "backfill_tenant_previews",
    "first_user_text",
]
