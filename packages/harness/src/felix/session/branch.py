"""Branch summarization when leaving a path via rewind/fork."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from felix.logging_setup import loggable
from felix.observability.metrics import record_counter
from felix.patterns.types import ChatMessage
from felix.session.compaction import (
    STRUCTURED_SUMMARY_PROMPT,
    extract_file_ops_from_events,
    serialize_conversation,
)
from felix.session.tree import (
    active_branch_events,
    fork_thread,
    get_event_id,
    get_parent_id,
    leaf_lock,
    rewind_to,
    stored_leaf,
)
from felix.session.types import AppendableEvent, Session, SessionEvent

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.session.branch")


def extract_file_ops(events: list[SessionEvent]) -> dict[str, list[str]]:
    return extract_file_ops_from_events(events)


def abandoned_events(
    events: list[SessionEvent],
    *,
    old_leaf_id: str | None,
    new_leaf_id: str,
    session_id: str = "",
) -> list[SessionEvent]:
    """Events on the old branch from common ancestor (exclusive) to old leaf."""
    if not old_leaf_id or old_leaf_id == new_leaf_id:
        return []
    old_path = active_branch_events(events, session_id=session_id, leaf_id=old_leaf_id)
    new_path = active_branch_events(events, session_id=session_id, leaf_id=new_leaf_id)
    old_ids = [get_event_id(e) for e in old_path]
    new_ids = {get_event_id(e) for e in new_path}
    # Deepest shared ancestor
    common: str | None = None
    for eid in reversed(old_ids):
        if eid and eid in new_ids:
            common = eid
            break
    out: list[SessionEvent] = []
    for e in old_path:
        eid = get_event_id(e)
        if common is None:
            out.append(e)
            continue
        if eid == common:
            continue
        # After common: include until end of old path
        # Walk: if we haven't hit common yet, skip; after common, include.
    # Recompute with explicit walk
    past_common = common is None
    out = []
    for e in old_path:
        eid = get_event_id(e)
        if not past_common:
            if eid == common:
                past_common = True
            continue
        out.append(e)
    return out


async def summarize_abandoned_branch(
    session: Session,
    *,
    old_leaf_id: str | None,
    new_leaf_id: str,
    model: Any | None = None,
    instructions: str | None = None,
    lock_held: bool = False,
) -> dict[str, Any] | None:
    """Append a branch_summary event at the new leaf if there is abandoned work.

    ``lock_held``: the caller is inside `tree.leaf_lock` for this thread (`rewind_and_persist`).
    """
    events = await session.get_events()
    abandoned = abandoned_events(
        events,
        old_leaf_id=old_leaf_id,
        new_leaf_id=new_leaf_id,
        session_id=getattr(session, "id", "") or "",
    )
    if not abandoned:
        return None

    file_ops = extract_file_ops(abandoned)
    summary_text: str | None = None
    usage: dict[str, Any] | None = None

    if model is not None:
        try:
            # Fenced, with the data notice, as the other two summarisers are: the abandoned
            # branch carries tool output like any transcript.
            from felix.session.compaction import _UNTRUSTED_NOTICE, clip_transcript, fence_untrusted

            text = fence_untrusted(clip_transcript(serialize_conversation(abandoned)))
            prompt = STRUCTURED_SUMMARY_PROMPT + _UNTRUSTED_NOTICE
            if instructions:
                prompt = f"{prompt}\n\nFocus: {instructions}"
            from felix.patterns.model import ModelChatOptions

            result = await model.chat(
                [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(role="user", content=text),
                ],
                [],
                ModelChatOptions(isolate_cache=True),
            )
            summary_text = result.message.content
            if getattr(result, "usage", None):
                u = result.usage
                usage = {
                    "input": getattr(u, "input", 0),
                    "output": getattr(u, "output", 0),
                    "cache_read": getattr(u, "cache_read", 0),
                    "cache_creation": getattr(u, "cache_creation", 0),
                }
        except Exception:
            logger.debug("branch summarization LLM failed", exc_info=True)

    if not summary_text:
        # Deterministic fallback
        n = len(abandoned)
        summary_text = (
            f"## Goal\nAbandoned branch ({n} events).\n\n"
            f"## Progress\n### Done\n- Left path from leaf {old_leaf_id}\n"
        )

    from felix.session.tree import annotate_and_append

    md: dict[str, Any] = {
        "type": "branch_summary",
        "fromId": old_leaf_id,
        "details": file_ops,
    }
    if usage:
        md["usage"] = usage
    ids = await annotate_and_append(
        session,
        [
            AppendableEvent(
                kind="branch_summary",  # type: ignore[arg-type]
                role="system",
                content=summary_text,
                metadata=md,
            )
        ],
        lock_held=lock_held,
    )
    return {
        "ok": True,
        "summary": summary_text,
        "event_id": ids[-1] if ids else None,
        "fromId": old_leaf_id,
        "details": file_ops,
    }


async def rewind_and_persist(
    session: Session,
    target_event_id: str,
    *,
    settings: Settings | None,
    tenant_id: str,
    summarize: bool,
    model: Any | None = None,
    instructions: str | None = None,
    restore_files: bool = False,
) -> dict[str, Any]:
    """Rewind ``session`` to ``target_event_id`` and store the leaf -- `/chat/rewind`'s sequence.

    Every step runs under the thread's `leaf_lock`: reading the leaf the rewind abandons,
    moving this process's leaf, the optional branch summary appended at the target, and each
    `persist_leaf`. Split, a turn's append on this replica could land between them and either
    parent on the pre-rewind leaf, or run its `store_leaf` after the rewind's `persist_leaf` and
    leave the row on the turn's event while this process's leaf says the target. Under the
    lock a concurrent turn's append lands wholly before the rewind (which then moves past it)
    or wholly after (parented on the target, or on the summary); the row and this process's
    leaf agree either way. The summary's model call is inside the hold, so a turn on the
    thread waits for it -- the summary describes a branch that must not move under it.

    A turn on *another* replica is not serialised by an in-process lock. Its append can be
    parented before the rewind commits and store its leaf after; what keeps that from
    overwriting the rewind is the row's `leaf_epoch`, which each `persist_leaf` here bumps and
    which that turn's `store_leaf` requires to be unchanged since its `sync_leaf`. Its write is
    skipped and its next turn starts from the rewound leaf; the appends it makes until then
    still land in the log, on the branch the rewind left.

    With ``restore_files``, the files the model's file tools changed on the abandoned branch are
    put back as they were before it (`tools/workspace_checkpoints.py`), still inside the hold, so
    no turn on this replica writes a file between the rewind and its restore; the outcome is
    ``files``. With or without it, that branch's file checkpoints are forgotten
    (`_settle_abandoned_files`).

    Returns `rewind_to`'s result, with ``branch_summary`` when one was written; an unknown
    target returns ``{"ok": False, ...}`` and moves nothing.
    """
    from felix.session.thread_state import persist_leaf, update_thread_meta

    thread_id = session.id
    async with leaf_lock(session):
        # The leaf the rewind abandons, which the branch summary describes. Read, not synced:
        # only the rewind itself moves this process's leaf.
        old_leaf = await stored_leaf(session)
        result = await rewind_to(session, target_event_id)
        if not result.get("ok"):
            return result
        await persist_leaf(
            settings=settings, tenant_id=tenant_id, thread_id=thread_id, leaf_event_id=target_event_id
        )
        await update_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id, phase="idle")
        result = dict(result)
        if settings is not None and old_leaf and old_leaf != target_event_id:
            files = await _settle_abandoned_files(
                session,
                settings,
                tenant_id,
                old_leaf=old_leaf,
                new_leaf=target_event_id,
                restore=restore_files,
            )
            if files is not None:
                result["files"] = files
        elif restore_files:
            from felix.tools.workspace_checkpoints import empty_report

            result["files"] = dict(empty_report())
        if not (summarize and old_leaf and old_leaf != target_event_id):
            return result
        branch_summary = None
        try:
            await update_thread_meta(
                settings=settings, tenant_id=tenant_id, thread_id=thread_id, phase="branch_summary"
            )
            branch_summary = await summarize_abandoned_branch(
                session,
                old_leaf_id=old_leaf,
                new_leaf_id=target_event_id,
                model=model,
                instructions=instructions,
                lock_held=True,
            )
            if branch_summary:
                await persist_leaf(
                    settings=settings,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    leaf_event_id=branch_summary.get("event_id") or target_event_id,
                )
        except Exception:
            logger.warning("branch summary failed for thread=%s", thread_id, exc_info=True)
            branch_summary = None
        await update_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id, phase="idle")
    out = dict(result)
    if branch_summary:
        out["branch_summary"] = branch_summary
    return out


async def _settle_abandoned_files(
    session: Session,
    settings: Settings,
    tenant_id: str,
    *,
    old_leaf: str | None,
    new_leaf: str,
    restore: bool,
) -> dict[str, Any] | None:
    """The file changes made on the branch a rewind abandons: put back first when `restore`, then
    forgotten either way, so a later rewind undoes only what changed after this one.

    A change belongs to the branch when it is anchored on one of its events, or at the event it
    branched from -- a change made while that event was the leaf came after it, and that event's
    tool calls do not run again. Forgetting is what keeps a second rewind to the same point from
    reaching back past the first: without it, the branched-from event's captures from the earlier
    branch would be read again and put a file back to before a change the person has since made.

    Returns the restore's report, or None without `restore`. A restore that fails answers
    `{"error": "unavailable"}` (fixed text) and forgets nothing, so a retry can still restore."""
    from felix.storage import get_object_store
    from felix.tools import checkpoint_ledger
    from felix.tools.workspace_checkpoints import empty_report, restore_files

    try:
        events = await session.get_events()
    except Exception:
        # The rewind has already been stored: answer it, never a 500 for a rewind that happened.
        logger.warning(
            "reading a rewound branch failed for thread=%s", loggable(session.id, limit=200), exc_info=True
        )
        record_counter("felix_file_checkpoint_failed", {"stage": "restore" if restore else "forget"})
        return {"error": "unavailable"} if restore else None
    abandoned = abandoned_events(
        events, old_leaf_id=old_leaf, new_leaf_id=new_leaf, session_id=getattr(session, "id", "") or ""
    )
    if not abandoned:
        return dict(empty_report()) if restore else None
    anchors = {eid for e in abandoned if (eid := get_event_id(e))}
    if branched_from := get_parent_id(abandoned[0]):
        anchors.add(branched_from)
    report: dict[str, Any] | None = None
    if restore:
        try:
            report = dict(await restore_files(settings, tenant_id, session.id, anchors))
        except Exception:
            logger.warning(
                "restoring files on rewind failed for thread=%s",
                loggable(session.id, limit=200),
                exc_info=True,
            )
            record_counter("felix_file_checkpoint_failed", {"stage": "restore"})
            return {"error": "unavailable"}
    try:
        await checkpoint_ledger.forget_anchored(
            settings, get_object_store(settings), tenant_id, session.id, anchors
        )
    except Exception:
        logger.warning(
            "forgetting a rewound branch's file checkpoints failed for thread=%s",
            loggable(session.id, limit=200),
            exc_info=True,
        )
        record_counter("felix_file_checkpoint_failed", {"stage": "forget"})
    return report


async def fork_and_persist(
    source: Session,
    dest: Session,
    *,
    settings: Settings | None,
    tenant_id: str,
    from_event_id: str | None = None,
) -> dict[str, Any]:
    """Fork ``source`` into a new thread ``dest`` and store its leaf -- `/chat/fork`'s sequence.

    The destination is named by the caller, so it must not exist yet: a fork into a live
    thread overwrote its leaf and spliced a second conversation into its log. One that has an
    event or a metadata row (`thread_state.thread_exists`) is refused with
    ``{"ok": False, "error": "thread_exists"}`` and nothing is written.

    The refusal is decided inside the destination's `leaf_lock` hold, with the copy and its
    `persist_leaf`: two forks to one new id on this replica serialise, and the second finds
    the first's thread. Across replicas the in-process lock serialises nothing, so the
    metadata row is claimed (`thread_state.claim_thread`, an insert that only one writer wins)
    before anything is copied. What that leaves open is a *turn* on another replica appending
    to the same never-used id in the moment between the check and the copy: a turn creates no
    row, so the claim does not see it. A client that names its own fresh id never does that.

    The source is only read (`stored_leaf`), never moved, so it is not locked: a turn
    mid-append there is copied up to the leaf its row held.
    """
    from felix.session.thread_state import (
        LAST_MANIFEST_KEY,
        PREVIEW_KEY,
        claim_thread,
        get_thread_meta,
        persist_leaf,
        thread_exists,
    )

    # The fork copies the source's conversation from its start, so it begins with the same
    # first message and was last run under the same manifest: the session index shows it so
    # rather than as a bare id until its first turn of its own.
    source_meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=source.id)
    # Not the pin itself (`manifest_name`, `manifest_hash`): the fork's own first turn takes it.
    inherited = {
        PREVIEW_KEY: source_meta.get(PREVIEW_KEY),
        LAST_MANIFEST_KEY: source_meta.get(LAST_MANIFEST_KEY) or source_meta.get("manifest_name"),
    }
    async with leaf_lock(dest):
        if await thread_exists(dest, settings=settings, tenant_id=tenant_id) or not await claim_thread(
            settings=settings,
            tenant_id=tenant_id,
            thread_id=dest.id,
            parent_session_id=source.id,
            **inherited,
        ):
            return {"ok": False, "error": "thread_exists", "thread_id": dest.id}
        result = await fork_thread(source, dest, from_event_id=from_event_id)
        await persist_leaf(
            settings=settings, tenant_id=tenant_id, thread_id=dest.id, leaf_event_id=result.get("leaf_id")
        )
    return result


__all__ = [
    "abandoned_events",
    "extract_file_ops",
    "fork_and_persist",
    "rewind_and_persist",
    "summarize_abandoned_branch",
]
