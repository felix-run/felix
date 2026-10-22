"""The head each thread's last append in this process left, for a live stream's `id:` cursor.

A live stream stamps its structural frames with the thread's head. Reading `head()` for each one
cost a transaction per frame -- two a plain turn, four more per tool call -- to learn what the
turn's own appends had just been told. The store records the head here as an append announces
itself (`store._announce`), and the stream reads it back (`known_head`).
"""

from __future__ import annotations

from felix.bounded_cache import BoundedCache

# Bounded: a thread that falls out is read from the store again.
_known_heads = BoundedCache(4096)
# Resets this process has made, of any thread. An append notes the count as it starts, and its head
# is kept only if no reset landed meanwhile: an append that committed just before a `reset` can
# announce just after it, and its head would then name events the reset deleted. Process-wide
# rather than per thread because resets are rare and a miss costs one `head()` read.
_resets = 0


def _key(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}\x00{thread_id}"


def resets() -> int:
    """The count an append notes before it writes, and hands back to `record_head`."""
    return _resets


def record_head(tenant_id: str, thread_id: str, next_seq: int, *, resets_seen: int) -> None:
    if resets_seen == _resets:
        _known_heads[_key(tenant_id, thread_id)] = next_seq


def note_reset(tenant_id: str, thread_id: str) -> None:
    """A thread's log was cleared: drop its head, and void every head an append in flight records."""
    global _resets
    _resets += 1
    _known_heads.pop(_key(tenant_id, thread_id))


def known_head(tenant_id: str, thread_id: str) -> int | None:
    """The `head()` this process's last append to the thread left, or None if it has none.

    Another replica's append since then is not in this number, so it falls behind the store, and
    a client resuming from it is sent an event it already had rather than missing one. A reset
    here drops the entry, and an append in flight across it records nothing; a reset on another
    replica mid-turn is the one way it can run ahead, until this turn's next append.
    """
    return _known_heads.get(_key(tenant_id, thread_id))


def reset_known_heads_for_tests() -> None:
    _known_heads.clear()
