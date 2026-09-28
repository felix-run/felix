"""Bounded in-process buffers for events that must survive a failed flush.

Audit and usage events are buffered in the emitting process and written in batches.
Two properties matter and neither is free:

* **A failed write must not lose the batch.** Draining before the write means one
  `commit()` failure discards those events permanently — for audit, that is the
  compliance record.
* **A buffer that cannot be flushed must not grow without bound.** If Postgres is
  unreachable for an hour, an unbounded list is a memory leak in a long-lived process.

`DurableBuffer` resolves the tension by re-queueing a failed batch and dropping the
*oldest* events once a ceiling is reached, counting every drop so the loss is visible
rather than silent.

Re-queueing has a failure of its own: an event the database will *never* accept is retried
forever, and every event behind it waits in the buffer until the ceiling starts dropping
them. `drain` tells the two failures apart. A batch that fails is written again one event at
a time; an event Postgres rejects as data (`DataError`, `IntegrityError`) is quarantined —
dropped, counted as `felix_buffer_quarantined`, logged with its id — and the rest go through.
Anything else is the database being unavailable, and stops the pass: the event it failed on
and everything after it go back to the front, as before, without a round trip each.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("felix.buffers")

# ~10k events is a few MB of dicts — enough to ride out a long outage, small enough
# that a permanently-broken database cannot exhaust the process.
DEFAULT_MAX_PENDING = 10_000


class DurableBuffer:
    """A size-capped FIFO of pending events with fail-safe drain semantics."""

    def __init__(self, name: str, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._name = name
        self._max = max_pending
        self._items: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._dropped = 0
        self._quarantined = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def dropped(self) -> int:
        """Events discarded because the buffer was full. Non-zero means data loss."""
        with self._lock:
            return self._dropped

    @property
    def quarantined(self) -> int:
        """Events set aside because the database rejected them as data. Each is data loss."""
        with self._lock:
            return self._quarantined

    def _quarantine(self, events: list[dict[str, Any]], exc: Exception) -> None:
        with self._lock:
            self._quarantined += len(events)
        try:
            from felix.observability.metrics import record_counter

            record_counter("felix_buffer_quarantined", {"buffer": self._name}, len(events))
        except Exception:  # pragma: no cover - metrics must never break a write path
            logger.debug("buffer quarantine counter failed", exc_info=True)
        # The id and the error class, never the payload: a row Postgres refused may be refusing
        # exactly because of what it carries.
        logger.error(
            "%s: quarantined %d event(s) the database rejected (%s): %s",
            self._name,
            len(events),
            type(exc).__name__,
            ", ".join(str(e.get("id") or "?") for e in events),
        )

    async def drain(self, write: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> int:
        """Write everything pending with `write`; returns how many events landed.

        On a failed batch, isolates what the database rejects from what it could not take
        (see the module docstring). Raises the unavailability error, after re-queueing, when
        one stopped the pass — the same signal a failed flush always gave its caller.
        """
        batch = self.take()
        if not batch:
            return 0
        try:
            await write(batch)
            return len(batch)
        except Exception as exc:
            if len(batch) == 1:
                if _rejected_as_data(exc):
                    self._quarantine(batch, exc)
                    return 0
                self.requeue(batch)
                raise
        written = 0
        for i, event in enumerate(batch):
            try:
                await write([event])
                written += 1
            except Exception as exc:
                if _rejected_as_data(exc):
                    self._quarantine([event], exc)
                    continue
                self.requeue(batch[i:])
                raise
        return written

    def _count_drop(self, n: int) -> None:
        """Losing audit or usage rows is a governance event, not a log line.

        The running total was already tracked and already logged, but nothing exported it,
        so silent data loss could only be found by reading logs after the fact.
        """
        try:
            from felix.observability.metrics import record_counter

            record_counter("felix_buffer_dropped", {"buffer": self._name}, n)
        except Exception:  # pragma: no cover - metrics must never break a write path
            logger.debug("buffer drop counter failed", exc_info=True)

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(event)
            over = len(self._items) - self._max
            if over > 0:
                for _ in range(over):
                    self._items.popleft()
                self._dropped += over
                dropped = self._dropped
            else:
                dropped = 0
        if dropped:
            self._count_drop(over)
            logger.error(
                "%s buffer full (max=%d); dropped oldest events, %d lost since start",
                self._name,
                self._max,
                dropped,
            )

    def __iter__(self):
        return iter(self.snapshot())

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.snapshot()[index]

    def __bool__(self) -> bool:
        return bool(len(self))

    def clear(self) -> None:
        """Discard everything pending without writing it."""
        with self._lock:
            self._items.clear()

    def take(self) -> list[dict[str, Any]]:
        """Atomically remove and return everything currently buffered."""
        with self._lock:
            batch = list(self._items)
            self._items.clear()
            return batch

    def requeue(self, batch: list[dict[str, Any]]) -> None:
        """Put a failed batch back at the front, preserving order.

        Events that arrived while the write was in flight stay after it, and the same
        ceiling applies — a batch that cannot ever be written is eventually dropped
        rather than pinning memory forever.
        """
        if not batch:
            return
        with self._lock:
            self._items.extendleft(reversed(batch))
            over = len(self._items) - self._max
            if over > 0:
                for _ in range(over):
                    self._items.popleft()
                self._dropped += over
                dropped = self._dropped
            else:
                dropped = 0
        if dropped:
            self._count_drop(over)
            logger.error(
                "%s buffer full while re-queueing a failed flush; %d lost since start",
                self._name,
                dropped,
            )

    def snapshot(self) -> list[dict[str, Any]]:
        """Copy the pending items without draining (tests and diagnostics)."""
        with self._lock:
            return list(self._items)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._items.clear()
            self._dropped = 0
            self._quarantined = 0


def _rejected_as_data(exc: BaseException) -> bool:
    """Whether the database refused the row itself — the same row will be refused every time.

    SQLSTATE class 22 (data exception) and 23 (integrity constraint), which SQLAlchemy raises as
    `DataError` and `IntegrityError`. Not `ProgrammingError`: a policy or privilege failure is
    configuration, true of every row, and quarantining would discard a whole outage's worth.
    """
    from sqlalchemy.exc import DataError, IntegrityError

    return isinstance(exc, (DataError, IntegrityError))


__all__ = ["DEFAULT_MAX_PENDING", "DurableBuffer"]
