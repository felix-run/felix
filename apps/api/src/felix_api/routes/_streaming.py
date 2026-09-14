"""Tailing a session log over SSE, and how fast to ask again.

Split out of `routes/chat.py`, which had grown to the largest module in the repo while a
sixth of it was this one subject: no route decorator among these names, and no importer
outside `chat.py` and its tests. Two streams use them — `GET /chat/stream/{thread_id}`,
which reattaches to a thread, and the durable arm of `POST /chat/stream`, which reports a
run the worker is executing. They are the same tail over the same log, and keeping the
pieces together is what stops the two drifting into meaning different things by the same
frame name.

The division against `_sse.py` is deliberate: that module knows the *envelope* — how a
frame is spelled, what `[DONE]` is, where a heartbeat goes. This one knows the *source* —
which rows to read, where the cursor is, and how long to wait before asking again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from felix.durability.fibers import FIBER_TERMINAL_STATUSES
from felix.logging_setup import loggable
from felix.session.notify import ThreadWatch, Wake, thread_watch
from felix.session.snapshot import gather_thread_snapshot
from felix.session.store import get_session_store
from felix.session.types import GetEventsOpts, Session, SessionEvent

from felix_api.errors import client_safe_message
from felix_api.routes._sse import DONE, KEEP_ALIVE, error_frame, frame

logger = logging.getLogger(__name__)

# Fiber statuses that mean the run will not change again: the fiber store's terminal set,
# plus the client-side cancel the stream reports itself.
RUN_TERMINAL = FIBER_TERMINAL_STATUSES | {"cancelled"}

# How long a stream stays at the floor before the poll starts decaying, and how sharply it
# decays after that.
#
# A plain exponential from the first empty round would be wrong here. Backing off costs
# first-event latency -- a thread that goes quiet and then produces makes the client wait
# up to the current delay -- and the moment a user is most likely to act is right after
# they reattach. So the first half-minute stays at the floor, and only a stream that has
# been silent past that decays. The load this is about comes from tabs left open for
# minutes, not from the first few seconds of one.
POLL_BACKOFF_GRACE_SECONDS = 30.0
POLL_BACKOFF_FACTOR = 1.5

# The ceiling a stream may decay to once notifications are actually being delivered.
# Far above the un-notified ceiling because the poll is then a safety net against a
# dropped pub/sub message rather than the mechanism itself: one query per minute per
# idle client instead of one every ten seconds.
NOTIFIED_POLL_CEILING_SECONDS = 60.0

# How many pending gates one durable stream will announce.
#
# Stated rather than inherited from `list_approvals`' default, because the ordering is
# `created_at DESC`: past this many pending rows on one thread the *oldest* stop being
# announced, and since a timed-out gate never leaves `pending` that is reachable by
# accumulation rather than by concurrency. A run blocks on one gate at a time, so a thread
# legitimately over this limit is a thread whose backlog an operator should be reading in
# `GET /approvals` anyway.
APPROVAL_ANNOUNCE_LIMIT = 50


def next_poll_delay(idle: float, delay: float, *, floor: float, ceiling: float) -> float:
    """The wait before the next poll of a quiet stream."""
    if idle < POLL_BACKOFF_GRACE_SECONDS:
        return floor
    return min(delay * POLL_BACKOFF_FACTOR, ceiling)


def session_event_frame(event: SessionEvent, cursor: int) -> str:
    """One `session_event` SSE frame.

    `id:` is the *next* sequence the client should expect, not the one it just got, so a
    client can hand it straight back as `Last-Event-ID`. Every `id:` on this stream means
    the same thing, including the snapshot's.

    **`tool_calls` and `tool_call_id` are what make a tool card renderable**, and this
    frame carried neither. A client folds these rows with the same function it folds a
    `snapshot` with: it reads `tool_calls` off an assistant message to open the cards, and
    `tool_call_id` off the tool message to close the matching one. Without them an
    assistant turn that called a tool folded to an empty message and its result was
    dropped outright, so a *warm* reattach — one that replays events rather than opening on
    a snapshot — silently rendered a transcript with no tool calls in it. The snapshot has
    carried all three since it was written (`session/snapshot.py:30-34`); only the
    incremental frame was thinner, which is why it read as a reattach quirk rather than a
    missing field. Adding them is a widening: a client that ignores them is unaffected.

    `id` is spelled the way the snapshot spells it, so a turn keeps one identity whether
    the client got it from a snapshot or from the tail.
    """
    md = dict(event.metadata or {})
    data: dict[str, Any] = {
        "id": md.get("event_id") or f"seq-{event.seq}",
        "seq": event.seq,
        "kind": event.kind,
        "role": event.role,
        "content": event.content,
        "name": event.name,
    }
    # Omitted rather than sent as null, matching the snapshot item: `eventsToTurns` tests
    # these for presence, and a null reads the same as absent to it either way.
    if event.tool_call_id:
        data["tool_call_id"] = event.tool_call_id
    if event.tool_calls:
        data["tool_calls"] = event.tool_calls
    return frame({"event": "session_event", "data": data}, cursor=cursor)


async def drain_session_events(reader: Session, cursor: int) -> tuple[list[str], int]:
    """Frames for everything appended since `cursor`, and the cursor after them.

    The one place that turns session log rows into `session_event` frames, shared by the
    reattach stream and the durable stream. They are the same tail over the same log, and
    the point of a single helper is that they cannot drift into meaning different things
    by the same frame name.

    `get_events(from_seq=...)` already applies `seq >= from_seq` in SQL on both backends,
    so filtering again in Python re-walks the page to discard nothing.
    """
    events = await reader.get_events(GetEventsOpts(from_seq=cursor))
    frames = []
    for event in events:
        cursor = event.seq + 1
        frames.append(session_event_frame(event, cursor))
    return frames, cursor


@dataclass(slots=True)
class ResumePacing:
    """How long a resume stream waits before asking again, and when it gives up.

    Split out of `chat_stream_resume` because the rules were interleaved line by line
    with SSE framing -- `yield f"id: {cursor}\ndata: ..."` two lines from the decay
    ceiling. Those are protocol and policy at different levels, and the comment density
    around them was what a missing seam looks like. Here the rules sit together and can
    be read as rules; the loop reads as protocol.

    Nothing about the behaviour changed: `saw_events`, `went_quiet`, `exhausted` and
    `waited` are the four things the loop body did, in the order it did them.
    """

    floor: float
    ceiling: float
    idle_limit: float
    #: The ceiling a stream may decay to *once wakes are being delivered*. The long
    #: default is right for a stream whose only source is the session log, because the
    #: poll is then a safety net rather than the mechanism. A caller that also polls
    #: something the notification does not cover has to pass its own -- being woken for
    #: one resource says nothing about the other. `durable_run_gen` is that caller.
    notified_ceiling: float = NOTIFIED_POLL_CEILING_SECONDS
    #: How long to wait for the next append. The loop reads this, never computes it.
    timeout: float = 0.0
    _idle: float = 0.0
    _notified: bool = False

    def __post_init__(self) -> None:
        self.timeout = self.floor

    @property
    def exhausted(self) -> bool:
        """Time to close rather than hold an idle connection open forever. The client
        reconnects with its `Last-Event-ID` and loses nothing."""
        return self._idle >= self.idle_limit

    def saw_events(self) -> None:
        """Backoff is a measure of idleness, so activity resets it -- otherwise a thread
        that goes quiet and then busy answers the next message at the decayed interval."""
        self._idle = 0.0
        self.timeout = self.floor

    def went_quiet(self) -> None:
        # `self.timeout`, not `self.floor`: the accounting has to follow the actual wait
        # or the idle limit stops meaning the number of seconds it says.
        self._idle += self.timeout
        # A notified stream polls only as a safety net, so it can afford a far longer
        # interval. When Redis drops, `by_notification` goes False on the next wait and
        # this tightens back on its own, without anything having to notice.
        ceiling = self.notified_ceiling if self._notified else self.ceiling
        self.timeout = next_poll_delay(self._idle, self.timeout, floor=self.floor, ceiling=ceiling)

    def observed(self, progressed: bool) -> None:
        """Fold one iteration's outcome in.

        Both streaming loops ran `saw_events()`/`went_quiet()` by hand. That is an ordering
        rule about a stateful object, and it belongs in one place rather than two.
        """
        if progressed:
            self.saw_events()
        else:
            self.went_quiet()

    def waited(self, wake: Wake) -> None:
        self._notified = wake.by_notification
        if wake.woken:
            self.saw_events()


async def stream_cursor(settings: Any, tenant_id: str, thread: str | None) -> int | None:
    """The next session sequence this thread will write.

    A cursor, not a last-seen id: a client hands it back as `Last-Event-ID` and gets
    everything from there on. Using the session log rather than a per-connection
    counter is what makes it mean anything to the *next* connection.
    """
    if not thread:
        return None
    try:
        head = await get_session_store(settings, tenant_id=tenant_id).open(thread).head()
        return int(head.get("seq") or 0)
    except Exception:
        logger.debug("stream cursor unavailable for %s", loggable(thread, limit=80), exc_info=True)
        return None


async def resume_stream_gen(
    *,
    settings: Any,
    tenant_id: str,
    thread: str,
    after: int | None,
    poll: float,
    poll_max: float,
    idle_limit: float,
) -> AsyncIterator[str]:
    """Tail a thread for a client that reattached to it.

    Cold reconnect (`after is None`) opens with a `snapshot` frame carrying the transcript;
    a warm one replays only the session events after that cursor. Both then tail the log,
    which is shared state, so this works regardless of which replica served the original
    turn.

    Sibling of `durable_run_gen`, and here for the same reason: it is the same tail over the
    same log, and the two drifting apart is exactly what this module exists to prevent. It
    took nothing from the request but seven scalars, so the route above it is now parse and
    delegate -- which is the shape the durable arm of `POST /chat/stream` already had.
    """
    cursor = after
    try:
        if cursor is None:
            snapshot = await gather_thread_snapshot(settings=settings, tenant_id=tenant_id, thread=thread)
            cursor = int((await stream_cursor(settings, tenant_id, thread)) or 0)
            # The snapshot carries every event so far, so the cursor it hands back is the
            # next sequence the client should expect — not the last one it has. Every `id:`
            # on this stream means the same thing, which is what lets a client hand it
            # straight back as `Last-Event-ID`.
            yield frame({"event": "snapshot", "data": snapshot}, cursor=cursor)

        store = get_session_store(settings, tenant_id=tenant_id)
        pacing = ResumePacing(floor=poll, ceiling=poll_max, idle_limit=idle_limit)
        # One subscription for the life of the stream. Waiting through a watch rather than
        # a call per iteration is what keeps this to a single SUBSCRIBE/UNSUBSCRIBE pair
        # instead of one per poll interval.
        async with thread_watch(tenant_id, thread) as watch:
            reader = store.open(thread)
            while True:
                frames, cursor = await drain_session_events(reader, cursor)
                for frame_text in frames:
                    yield frame_text
                pacing.observed(bool(frames))
                if pacing.exhausted:
                    break
                yield KEEP_ALIVE
                # Wait for the thread to move rather than sleeping through it. The query
                # above runs either way, so a dropped notification costs latency and never
                # correctness -- which is what lets the ceiling relax rather than disappear.
                pacing.waited(await watch.wait(timeout=pacing.timeout))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("chat resume failed thread=%s", loggable(thread, limit=80))
        yield error_frame(client_safe_message(exc))
    yield DONE


def durable_thread(tenant_id: str, accepted: dict[str, Any]) -> str:
    """The thread a durable run writes its transcript to, or "" if it cannot be derived.

    A run started without a `thread_id` is not a run without a thread: the fiber mints
    `{tenant}:fiber:{id}` and writes there just the same, so deriving the same id is what
    lets an anonymous durable run report progress at all.
    """
    from felix.durability.fibers import fiber_thread_id

    thread = str(accepted.get("thread_id") or "")
    if thread:
        return thread
    fiber_id = str(accepted.get("fiber_id") or accepted.get("resume_token") or "")
    return fiber_thread_id(tenant_id, fiber_id) if fiber_id else ""


def _has_lapsed(expires_at: Any, now_ms: float) -> bool:
    """Whether a gate's deadline has passed. Never raises on a malformed value.

    `drain` promises it never raises, and the promise has to survive the comparison as well as
    the store read. `expires_at` is `BigInteger` on both arms today, so a non-numeric value is
    unreachable — but the alternative to this guard is an exception escaping mid-body, and
    `durable_run_gen` has no top-level handler, so a client would see a truncated connection
    with no `error:` frame and no `[DONE]`: the exact failure `error_frame` exists to prevent.
    An unreadable deadline means "I cannot say it lapsed", which keeps the prompt visible.
    """
    if expires_at is None:
        return False
    try:
        return float(expires_at) < now_ms
    except TypeError, ValueError:
        return False


def approval_required_frame(row: dict[str, Any]) -> str:
    """One `approval_required` SSE frame, rebuilt from a pending approvals row.

    The same eight keys `manifests/builder.py` emits as a side event, so a client folds a
    durable run's prompt with the handler it already has. Every one of them is on the row as
    of felix#245 — `reason` and `tool_call_id` were added there precisely so the frame could
    be *re-derived* rather than forwarded, which is what lets this path exist without a
    second delivery channel.

    Safe to build from the row **only because the caller filtered by thread**. A pending row
    is reused across threads and names whichever call opened it, so a row fetched without
    that filter could carry another conversation's `thread_id` and `tool_call_id`. The two
    facts are one fact: we read only rows this thread originated, so the row's provenance is
    this thread's provenance.
    """
    return frame(
        {
            "event": "approval_required",
            "data": {
                "approval_id": row.get("id") or "",
                "tool_name": row.get("tool_name") or "",
                "args": row.get("args") or {},
                "rule_id": row.get("rule_id") or "",
                "reason": row.get("reason") or "",
                "thread_id": row.get("thread_id") or "",
                "tool_call_id": row.get("tool_call_id") or "",
                "expires_at": row.get("expires_at"),
            },
        }
    )


class DurableTail:
    """What a durable run has newly produced — its transcript, and anything it is blocked on.

    "Is there a tail?" was answered in four places: a two-element return whose
    "both are None" invariant no type could state, an `is not None` branch in the loop, a
    try/except-degrade inside the loop body, and a null watch class beside it. The loop
    should ask for frames and for a wait, and never learn the answer — so the null case
    lives here, once, and the protocol loop reads as protocol. Adding the approvals source
    keeps that shape: `drain` still answers one question.

    The reader is opened once rather than per iteration, which would put a store
    construction on the hot path of every poll. It owns the cursor for the same reason
    `drain_session_events` returns one: two call sites keeping a loop-local in sync is
    how a tail starts replaying or skipping. `_announced` is the same idea for approvals,
    which have no cursor — the store answers "what is pending", not "what is new".
    """

    __slots__ = (
        "_announced",
        "_cursor",
        "_may_read_approvals",
        "_reader",
        "_settings",
        "_tenant_id",
        "_thread",
        "_watch",
    )

    def __init__(
        self,
        *,
        reader: Session | None,
        thread: str,
        cursor: int,
        watch: ThreadWatch | None,
        settings: Any = None,
        tenant_id: str = "",
        may_read_approvals: bool = False,
    ) -> None:
        self._reader = reader
        self._thread = thread
        self._cursor = cursor
        self._watch = watch
        self._settings = settings
        self._tenant_id = tenant_id
        self._may_read_approvals = may_read_approvals
        self._announced: set[str] = set()

    async def drain(self) -> list[str]:
        """Frames for everything new since the last drain. Never raises.

        A read that fails degrades to status-only for that iteration rather than failing
        the run stream: the answer still arrives on `final`, which is all this endpoint
        promised before. The cursor is untouched on failure, so the next poll re-reads
        the same range instead of skipping it.
        """
        if self._reader is None:
            return []
        frames: list[str] = []
        try:
            frames, self._cursor = await drain_session_events(self._reader, self._cursor)
        except Exception:
            logger.debug("durable tail read failed for %s", loggable(self._thread, limit=80), exc_info=True)
        return frames + await self._drain_approvals()

    async def _drain_approvals(self) -> list[str]:
        """Frames for approvals this run is blocked on that the client has not been told about.

        The session log does not carry these — `_append_produced` writes assistant turns and
        tool results, and an approval request is neither — so the transcript tail cannot
        reach them. The approvals table can: it is the same shared, cross-replica store, and
        since felix#232 a row names the thread it blocked.

        **Not a pub/sub bridge, for the reason the tail itself is not one.** `notify.py` can
        afford at-most-once delivery because a dropped wake still re-reads Postgres; here the
        message *would be* the frame, so a drop means the run blocks its whole TTL and then
        denies with nobody ever asked. Re-deriving from the row keeps "the notification is a
        hint, never the source of truth" true for this path too — and a client that attaches
        *after* the gate fired still sees the prompt, which a bus cannot offer.

        Deduped by id rather than by cursor: `list_approvals` answers "what is pending now",
        so the same row comes back every poll until it is decided, and a client re-shown a
        prompt it has already answered is worse than one shown it late.
        """
        if not self._thread or self._settings is None or not self._may_read_approvals:
            return []
        try:
            from felix.approvals.store import list_approvals

            rows = await list_approvals(
                self._settings,
                self._tenant_id,
                status="pending",
                thread_id=self._thread,
                limit=APPROVAL_ANNOUNCE_LIMIT,
            )
        except Exception:
            logger.debug(
                "durable approval read failed for %s", loggable(self._thread, limit=80), exc_info=True
            )
            return []
        now = time.time() * 1000
        frames = []
        for row in rows:
            approval_id = str(row.get("id") or "")
            if not approval_id or approval_id in self._announced:
                continue
            # An expired gate is history, not a question -- the same reason a *decided* one is
            # not announced. Nothing moves a timed-out row off `pending`: `wait_for_decision`
            # returns a denial and the caller writes nothing back, and `approvals` is not in
            # the retention sweep, so these accumulate forever. `find_approved` filters expiry
            # in its own WHERE for the authorization half; this is the display half of the
            # same fact, and without it a stream opens with a prompt that timed out weeks ago.
            #
            # `now` is *this* host's clock while `expires_at` was written by whichever process
            # created the row -- the worker, on a durable run. Skew larger than the gate's TTL
            # would suppress announcements the poll still lists, which is an NTP problem rather
            # than something to compensate for here, but it is the reason the poll stays the
            # channel of record.
            if _has_lapsed(row.get("expires_at"), now):
                continue
            self._announced.add(approval_id)
            frames.append(approval_required_frame(row))
        return frames

    async def wait(self, *, timeout: float) -> Wake:
        """Wait for the thread to move, or just wait, when there is no thread to watch.

        A wake here means the *session log* moved; an approval landing publishes nothing, so
        it is found by the poll underneath rather than announced. That is why the durable
        loop keeps its notified ceiling at `poll_max` instead of the long one — see
        `durable_run_gen`.
        """
        if self._watch is None:
            await asyncio.sleep(timeout)
            return Wake(woken=False, by_notification=False)
        return await self._watch.wait(timeout=timeout)


@contextlib.asynccontextmanager
async def durable_tail(
    settings: Any,
    tenant_id: str,
    accepted: dict[str, Any],
    from_seq: int | None,
    *,
    may_read_approvals: bool = False,
) -> AsyncIterator[DurableTail]:
    """Open the tail for a durable run, and hold its watch for the life of the stream.

    `from_seq` of None means the caller could not read the thread's head, which is not the
    same as zero: tailing from zero would re-emit the thread's entire prior transcript as
    if this run had produced it. There is no bound on that — `GetEventsOpts` has no default
    limit — so a client appending the frames would duplicate the whole conversation. Not
    knowing where to start is a reason not to tail, and the run still reports status and
    its answer.

    A watch is opened only when there is something to watch. Subscribing to a channel
    nothing can publish to would hold a refcount on the shared pub/sub connection for the
    life of the stream *and* report `by_notification=True`, telling the pacing it is safe
    to stretch its interval on a stream that can never be woken.
    """
    idle = DurableTail(reader=None, thread="", cursor=0, watch=None)
    thread = durable_thread(tenant_id, accepted) if from_seq is not None else ""
    if not thread:
        yield idle
        return
    try:
        reader = get_session_store(settings, tenant_id=tenant_id).open(thread)
    except Exception:
        logger.debug("durable tail unavailable for %s", loggable(thread, limit=80), exc_info=True)
        yield idle
        return
    async with thread_watch(tenant_id, thread) as watch:
        yield DurableTail(
            reader=reader,
            thread=thread,
            cursor=int(from_seq or 0),
            watch=watch,
            settings=settings,
            tenant_id=tenant_id,
            may_read_approvals=may_read_approvals,
        )


async def durable_run_gen(
    *,
    settings: Any,
    tenant_id: str,
    accepted: dict[str, Any],
    from_seq: int | None = None,
    may_read_approvals: bool = False,
) -> AsyncIterator[str]:
    """Stream a durable run's progress instead of pretending it is synchronous.

    `POST /chat` honours `spec.execution.mode: durable` and returns 202 with a
    `resume_token`; this endpoint did not mention it at all, so a manifest that asked
    for durable execution got it on one route and was silently ignored on the other.

    Streaming the run rather than returning 202 keeps the SSE contract a caller of this
    endpoint already has, and it delivers what durable is actually for: a disconnect
    here tears down the *poll*, not the run. That is the opposite of the transient
    path, where a hung-up client deliberately kills the run so it stops burning tokens.

    The first frame carries the `resume_token`, so a client that drops before the run
    finishes can come back to `GET /chat/runs/{token}` rather than starting over.

    **What the run is doing, not just that it is running.** The fiber invokes the agent
    through `invoke`, which runs the loop with `emit_events=False` — the deltas and the
    `on_tool_start`/`on_tool_end` pairs are dropped at the source, so there is no display
    event stream in the worker to forward and no transport that could carry one. What the
    fiber *does* produce is the session log: `_append_produced` writes each assistant turn
    and its tool results as they land, outside every `emit_events` guard. So this tails the
    log, exactly as `GET /chat/stream/{thread_id}` does, through `drain_session_events`.
    Shared state and one notification channel, no second delivery path for data already
    durably written.

    The consequence to know is that only *completed* messages are persisted, never chunks:
    a durable run yields tool cards and whole assistant messages, and never token deltas.
    That is the right trade for the mode whose point is that nobody is watching.

    **And what it is blocked on.** An approval request is the one thing the session log does
    not carry — `_append_produced` writes assistant turns and tool results, and a request for
    permission is neither — so the transcript tail cannot reach it, on precisely the path
    where a human has time to answer. The approvals table can, and does: `DurableTail` reads
    the rows pending on this thread and rebuilds `approval_required` from them. Same
    principle as the transcript, applied to the other half: re-derive from the durable record
    rather than forward a message, so a dropped anything costs latency instead of a decision.

    An approval landing publishes no thread notification, so it is found by the poll rather
    than announced — which is the second reason this loop keeps `notified_ceiling=poll_max`
    below rather than the long default.
    """
    from felix.durability.runs import get_durable_run

    try:
        token = str(accepted.get("resume_token") or "")
        yield frame({"event": "run_accepted", "data": accepted})

        poll = float(getattr(settings, "stream_resume_poll_seconds", 1.0) or 1.0)
        poll_max = max(poll, float(getattr(settings, "stream_resume_poll_max_seconds", 10.0) or 10.0))
        # The run's own TTL bounds this, not the resume stream's idle limit: a durable run
        # that outlives its expiry is finished either way, and holding the connection past
        # that point would keep a worker busy for a result that can no longer arrive. So the
        # pacing here is asked for its interval and never for `exhausted` — hence the
        # infinite idle limit rather than a second set of backoff rules.
        deadline = float(accepted.get("expires_at") or 0) or None

        # `notified_ceiling=poll_max`, deliberately, and this is the one place that needs to
        # say why. The long notified ceiling exists because a stream whose only source is the
        # session log polls as a safety net once wakes are being delivered. This loop polls a
        # *second* resource — the durable run row — and the fiber's status write publishes no
        # thread notification, so a wake being delivered says nothing about it. Left at the
        # default, a run that appends nothing for a few minutes (queued, a long tool call, a
        # fiber that died before its first append) would report its status up to a minute late.
        pacing = ResumePacing(floor=poll, ceiling=poll_max, idle_limit=math.inf, notified_ceiling=poll_max)
        last_status = ""

        async with durable_tail(
            settings, tenant_id, accepted, from_seq, may_read_approvals=may_read_approvals
        ) as tail:
            while True:
                run = await get_durable_run(settings, tenant_id, token)
                if run is None:
                    yield error_frame(f"run_not_found:{token}", kind="run_error")
                    break
                status = str(run.get("status") or "")
                # Drained *after* the status read and *before* the terminal check, so a run
                # that completed between two iterations still emits the turns it appended
                # before it flipped the row. The fiber writes the transcript and then saves
                # `completed`, so this ordering is what makes "every event, then `final`" true
                # rather than usually true.
                frames = await tail.drain()
                for frame_text in frames:
                    yield frame_text
                progressed = bool(frames)
                if status != last_status:
                    last_status = status
                    progressed = True
                    yield frame({"event": "run_status", "data": {"status": status, "resume_token": token}})
                if status in RUN_TERMINAL:
                    if status == "completed":
                        yield frame({"event": "final", "data": run.get("final") or {}})
                    else:
                        yield error_frame(str(run.get("error") or status), kind="run_error")
                    break
                if deadline and time.time() * 1000 >= deadline:
                    # Says which it was. "expired" and "still running" look identical to a
                    # client that only sees the stream close.
                    yield error_frame(f"run_expired:{token}", kind="run_error")
                    break
                pacing.observed(progressed)
                yield KEEP_ALIVE
                # Wait for the thread to move rather than sleeping through it. The status
                # query above runs either way, so a dropped notification costs latency and
                # never correctness -- the same property the reattach stream relies on.
                pacing.waited(await tail.wait(timeout=pacing.timeout))
    except asyncio.CancelledError:
        # The client hung up. The *run* survives -- that is what durable means -- so let the
        # cancellation through untouched rather than reporting a failure for work still going.
        raise
    except Exception as exc:
        # Mirrors `resume_stream_gen`. Without it an unexpected raise ends the body mid-stream
        # under an already-sent 200, with no `error:` frame and no `[DONE]`, and a client
        # cannot tell a truncated connection from a finished one. The sibling loop has had
        # this since it was written; this one did not, which is an asymmetry rather than a
        # decision.
        logger.exception("durable run stream failed token=%s", loggable(token, limit=80))
        yield error_frame(client_safe_message(exc), kind="run_error")
    yield DONE


# What other modules may use. `DurableTail`, `durable_tail`, `durable_thread` and
# `session_event_frame` are deliberately absent: each is reached only from inside this file,
# and exporting the implementation of an implementation is how the next helper ends up
# imported into `openai_compat.py` by accident rather than by decision.
__all__ = [
    "NOTIFIED_POLL_CEILING_SECONDS",
    "POLL_BACKOFF_FACTOR",
    "POLL_BACKOFF_GRACE_SECONDS",
    "RUN_TERMINAL",
    "ResumePacing",
    "drain_session_events",
    "durable_run_gen",
    "next_poll_delay",
    "resume_stream_gen",
    "stream_cursor",
]
