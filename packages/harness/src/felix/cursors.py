"""Keyset paging for time-ordered listings.

A cursor that carries only a timestamp cannot page a history that has ties in it, and
millisecond timestamps tie constantly: a single turn writes a user event, a tool call and a
final response microseconds apart. `WHERE ts < :last_ts` then steps over *every* row sharing
the boundary timestamp, so those rows are returned by no page at all.

That is how the audit and usage listings lost rows. Five events in one millisecond, read two
at a time, returned two — silently, with a well-formed response each time, which is the worst
way for a compliance record to be wrong.

The fix is to make the sort key unique by pairing the timestamp with the row id, and to
compare the pair. This module owns the whole rule rather than only the string format, because
two stores implementing it in parallel is how the halves drift: audit and usage had already
grown a `next_cursor` predicate that differed between their own memory and Postgres arms.

One caveat, latent rather than live: `id` is text, so Postgres orders it by the database
collation and the in-memory twin orders it by Python code point. Those agree for the ids
actually written — `uuid4().hex` is lowercase hex, which sorts identically under every common
collation — but `record_event` accepts a caller-supplied id, and under a non-C collation an id
outside that alphabet could order differently on the two backends. Paging stays correct and
complete either way, because each backend is self-consistent; only the order between two rows
in the same millisecond could differ.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import ColumnElement
    from sqlalchemy.sql.elements import ColumnElement as _ColumnElement

logger = logging.getLogger("felix.cursors")

SEPARATOR = ":"
Position = tuple[int, str]

_warned_legacy = False


class InvalidCursor(ValueError):
    """A cursor a client sent that this deployment cannot read.

    Its own type, so a route can catch exactly this and not the next `ValueError` a store
    grows underneath it — and its message is written for a client to read, which is what
    `client_safe_message(..., authored_for_clients=True)` asserts at the relay.
    """


def encode_cursor(ts: int, row_id: str) -> str:
    """The position of the last row on a page."""
    return f"{ts}{SEPARATOR}{row_id}"


def decode_cursor(cursor: str) -> Position:
    """Parse a cursor, tolerating the timestamp-only form this replaced.

    A bare `"1700000000000"` is read as `(ts, "")`, which compares less than every real
    `(ts, id)` at that timestamp — the old, lossy behaviour. That is deliberate: a cursor
    issued by the previous version is in flight for as long as one client holds one, and
    reproducing its behaviour is better than raising at it or, worse, re-reading rows the
    caller has already seen.

    It warns once per process, because otherwise nobody can ever answer "is it safe to delete
    this branch yet?" — a compatibility path with no signal is a permanent one.
    """
    global _warned_legacy

    raw_ts, found, row_id = cursor.partition(SEPARATOR)
    try:
        ts = int(raw_ts)
    except ValueError as exc:
        from felix.logging_setup import loggable

        raise InvalidCursor(f"cursor is not a page position: {loggable(cursor, limit=64)}") from exc
    if not found and not _warned_legacy:
        _warned_legacy = True
        logger.warning(
            "a timestamp-only cursor was used; it cannot address rows sharing its millisecond "
            "and is kept only for cursors issued before keyset paging landed"
        )
    return ts, row_id


def position_of(row: Any) -> Position:
    """The sort key of a row, from either shape a store holds it in.

    The twin keeps dicts and the store maps ORM objects, and both page on the same pair.
    """
    if isinstance(row, dict):
        return int(row["ts"]), str(row["id"])
    return int(row.ts), str(row.id)


def keyset_order(ts_col: ColumnElement[Any], id_col: ColumnElement[Any]) -> tuple[Any, Any]:
    """`ORDER BY ts DESC, id DESC` — the total order the cursor addresses positions in."""
    return ts_col.desc(), id_col.desc()


def keyset_before(
    ts_col: ColumnElement[Any], id_col: ColumnElement[Any], cursor: str
) -> _ColumnElement[bool]:
    """`(ts, id) < (cursor_ts, cursor_id)`, as a Postgres row comparison.

    A row constructor, not an `AND` chain: Postgres compares it lexicographically, which is
    what Python does to the tuple on the other arm, so the two agree by construction.
    """
    from sqlalchemy import tuple_

    return tuple_(ts_col, id_col) < decode_cursor(cursor)


def order_and_seek[Row](rows: Sequence[Row], cursor: str | None) -> list[Row]:
    """Sort newest-first and drop everything at or past the cursor — the twin's half.

    The store gets this from the database; this is the same rule written once so the two
    cannot disagree about which rows a cursor excludes.
    """
    out = list(rows)
    if cursor is not None:
        before = decode_cursor(cursor)
        out = [row for row in out if position_of(row) < before]
    out.sort(key=position_of, reverse=True)
    return out


def take_page[Row](
    rows: Sequence[Row],
    *,
    limit: int,
    position: Callable[[Row], Position] = position_of,
) -> tuple[list[Row], str | None]:
    """Cut a page and say where it ended, for both arms.

    `rows` is the full ordered remainder for the twin and `limit + 1` rows for the store; the
    has-more test is `len(rows) > limit` either way. The `and out` guard matters at `limit=0`,
    where the twin used to return no cursor and the store raised `IndexError` on an empty
    page — the shape of divergence the conformance arm exists to catch.
    """
    out = list(rows[:limit])
    more = len(rows) > limit
    return out, (encode_cursor(*position(out[-1])) if more and out else None)


__all__ = [
    "InvalidCursor",
    "decode_cursor",
    "encode_cursor",
    "keyset_before",
    "keyset_order",
    "order_and_seek",
    "position_of",
    "take_page",
]
