"""Split a document into retrievable chunks.

Retrieval returns chunks, not documents, so chunking decides what an agent can actually be
handed. Two properties matter more than cleverness:

*A chunk should be readable alone.* It arrives in a context window with no neighbours, so a
split through the middle of a sentence costs more than a slightly uneven size. Boundaries are
preferred in order — blank line, then line, then sentence, then whitespace — and only a run of
text with none of those is cut mid-token.

*Chunks overlap.* A fact that straddles a boundary is otherwise unfindable: neither chunk
contains it whole, so neither ranks for it. The overlap is carved off the end of the previous
chunk rather than added to the budget, so `max_chars` stays a real ceiling.

Deliberately not a token-aware splitter. That would need a tokenizer, which means either a
heavy dependency behind an extra or a per-model dependency in a layer that is meant to be
model-agnostic — and characters are within a small constant of tokens for prose, which is what
a retrieval budget needs.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1_500
DEFAULT_OVERLAP_CHARS = 150

# Below this a "chunk" is a fragment that costs a retrieval slot and answers nothing. A tail
# shorter than this is folded back into the previous chunk instead of standing alone.
#
# Applied as `min(MIN_TAIL_CHARS, max_chars // 3)`, not absolutely. At a small `max_chars`
# the flat floor swallowed the budget: with `max_chars=45` every tail was under 64, so the
# fold ran every time and returned one 80-character chunk for a 45-character ceiling —
# a budget quietly repealed by the tidying rule meant to serve it.
MIN_TAIL_CHARS = 64

# Searched backwards from the end of the window, best first. A blank line is a paragraph
# break and almost always a clean seam; a bare space is the last resort before cutting a word.
_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", "; ", " ")

# How far back from the window's end a boundary may be before taking it costs more (in wasted
# budget) than cutting mid-sentence. A quarter of the window is the usual rule of thumb.
_MAX_BACKTRACK_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str
    # Character offsets into the source document, so a hit can be traced back to where it
    # came from rather than only to which document it came from.
    start: int
    end: int


def _split_point(text: str, window_end: int, floor: int) -> int:
    """The best boundary at or before `window_end`, or `window_end` itself."""
    best = -1
    for sep in _BOUNDARIES:
        found = text.rfind(sep, floor, window_end)
        if found > best:
            best = found + len(sep)
            # Boundaries are ordered best-first, so the first acceptable one wins rather
            # than the latest-occurring one: a paragraph break slightly further back beats
            # a bare space nearer the limit.
            break
    return best if best > floor else window_end


def _overlap_start(text: str, end: int, overlap: int) -> int:
    """Where the next chunk begins: `end - overlap`, snapped forward to a word boundary.

    Taken literally, the overlap lands mid-word — a chunk beginning "ss guard resolves"
    instead of "egress guard resolves". That costs twice: the lexical channel tokenises "ss"
    as a word that matches nothing, and a model handed the chunk reads a truncated first term
    as if it were the text. Snapping forward rather than back keeps the chunk inside its
    budget; if there is no whitespace in the overlap window, the raw position stands, because
    a run with no spaces has no better answer.
    """
    if overlap <= 0:
        return end
    raw = max(end - overlap, 0)
    space = text.find(" ", raw, end)
    return space + 1 if space != -1 else raw


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split `text` into overlapping chunks, each at most `max_chars`.

    Empty or whitespace-only input yields no chunks — a document with nothing in it should
    not occupy a row and rank for nothing.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    # An overlap at or above the window would never advance the cursor, so this is a
    # termination condition rather than a preference.
    overlap = max(0, min(overlap_chars, max_chars // 2))

    body = text.strip()
    if not body:
        return []
    if len(body) <= max_chars:
        return [Chunk(index=0, text=body, start=0, end=len(body))]

    chunks: list[Chunk] = []
    cursor = 0
    while cursor < len(body):
        window_end = min(cursor + max_chars, len(body))
        if window_end == len(body):
            piece = body[cursor:]
            min_tail = min(MIN_TAIL_CHARS, max_chars // 3)
            prev_start = chunks[-1].start if chunks else 0
            # Fold a runt tail into its predecessor rather than emitting a chunk too small to
            # answer anything — but only when the merge stays inside the budget plus one runt.
            # Unconditionally, this repeals `max_chars` at small budgets.
            foldable = len(body) - prev_start <= max_chars + min_tail
            if chunks and len(piece.strip()) < min_tail and foldable:
                prev = chunks.pop()
                merged = body[prev.start :]
                chunks.append(Chunk(index=prev.index, text=merged.strip(), start=prev.start, end=len(body)))
            elif piece.strip():
                chunks.append(Chunk(index=len(chunks), text=piece.strip(), start=cursor, end=len(body)))
            break

        floor = cursor + int(max_chars * (1 - _MAX_BACKTRACK_RATIO))
        end = _split_point(body, window_end, floor)
        piece = body[cursor:end]
        if piece.strip():
            chunks.append(Chunk(index=len(chunks), text=piece.strip(), start=cursor, end=end))
        # Always advance, even if the boundary search returned something degenerate: a
        # cursor that fails to move is an infinite loop on a pathological document.
        cursor = max(_overlap_start(body, end, overlap), cursor + 1)
    return chunks


__all__ = ["DEFAULT_MAX_CHARS", "DEFAULT_OVERLAP_CHARS", "MIN_TAIL_CHARS", "Chunk", "chunk_text"]
