"""Keyset cursors for time-ordered listings.

A cursor that carries only a timestamp cannot page a history that has ties in it, and
millisecond timestamps tie constantly: a single turn writes a user event, a tool call and a
final response microseconds apart. `WHERE ts < :last_ts` then steps over *every* row sharing
the boundary timestamp, so those rows are returned by no page at all.

That is how the audit and usage listings lost rows. Five events in one millisecond, read two
at a time, returned two — silently, with a well-formed response each time, which is the worst
way for a compliance record to be wrong.

The fix is to make the sort key unique by pairing the timestamp with the row id, and to
compare the pair. Both stores order by `(ts, id)` descending and page on `(ts, id) < (last_ts,
last_id)`, which is a strict total order, so every row is on exactly one page.
"""

from __future__ import annotations

SEPARATOR = ":"


def encode_cursor(ts: int, row_id: str) -> str:
    """The position of the last row on a page."""
    return f"{ts}{SEPARATOR}{row_id}"


def decode_cursor(cursor: str) -> tuple[int, str]:
    """Parse a cursor, tolerating the timestamp-only form this replaced.

    A bare `"1700000000000"` is read as `(ts, "")`, which compares less than every real
    `(ts, id)` at that timestamp — the old, lossy behaviour. That is deliberate: a cursor
    issued by the previous version is in flight for as long as one client holds one, and
    reproducing its behaviour is better than raising at it or, worse, re-reading rows the
    caller has already seen.

    Raises `ValueError` on a cursor whose timestamp is not an integer, which is what an
    invented cursor looks like; callers turn that into a 400 rather than an empty page.
    """
    raw_ts, _, row_id = cursor.partition(SEPARATOR)
    return int(raw_ts), row_id


__all__ = ["decode_cursor", "encode_cursor"]
