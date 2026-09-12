"""The keyset cursor, including the old form it has to keep reading.

`felix/cursors.py` exists because a cursor carrying only a timestamp cannot page a history with
ties in it, and millisecond timestamps tie constantly. The pair `(ts, id)` is a strict total
order over rows, so every row falls on exactly one page.

The legacy branch is the one worth pinning: a cursor issued by the previous version is in
flight for as long as a client holds one, and the choice made for it — reproduce the old,
lossy behaviour rather than raise or re-read — is a decision, not an accident.
"""

from __future__ import annotations

import pytest
from felix.cursors import decode_cursor, encode_cursor, order_and_seek


def test_a_cursor_round_trips() -> None:
    assert decode_cursor(encode_cursor(1_700_000_000_000, "abc123")) == (1_700_000_000_000, "abc123")


def test_the_pair_orders_rows_sharing_a_timestamp() -> None:
    """The property the stores rely on: comparable, and total within one millisecond."""
    rows = [{"ts": 100, "id": "c"}, {"ts": 100, "id": "a"}, {"ts": 99, "id": "z"}, {"ts": 100, "id": "b"}]

    ordered = order_and_seek(rows, encode_cursor(100, "b"))

    # Everything strictly below the cursor position, newest first — including the two rows in
    # the same millisecond as the cursor, which a timestamp-only cursor would have skipped.
    assert [(r["ts"], r["id"]) for r in ordered] == [(100, "a"), (99, "z")]


def test_a_timestamp_only_cursor_still_reads() -> None:
    """The form the previous version issued, which a client may still be holding.

    It decodes to `(ts, "")`, which is below every real `(ts, id)` at that timestamp — so it
    skips the whole millisecond, exactly as it used to. Losing those rows is the old bug; a
    cursor that raised, or that re-read rows the caller had already seen, would be a new one.
    """
    position = decode_cursor("1700000000000")

    assert position == (1_700_000_000_000, "")
    assert position < (1_700_000_000_000, "anything")


def test_an_invented_cursor_is_refused_rather_than_returning_an_empty_page() -> None:
    """A silent empty page would read as "no more history" to a caller that asked wrongly."""
    for invented in ("not-a-cursor", "", "abc:def", ":x"):
        with pytest.raises(ValueError):
            decode_cursor(invented)


def test_an_id_containing_the_separator_keeps_its_tail() -> None:
    """Ids are hex today, but the split has to be on the first separator either way.

    Splitting on the last one, or on all of them, would truncate an id and page from a
    position no row occupies — which reads as a short history rather than an error.
    """
    assert decode_cursor("100:a:b:c") == (100, "a:b:c")
