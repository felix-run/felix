**`GET /usage/summary` builds its rows through a named serializer.** The response was
assembled inline in both arms of `felix.usage.store`, which made the shape unreadable to
anything outside the process: `felix-web`'s payload guard reads `_<row>_dict` functions to
learn what a route actually sends, so this area could not be guarded at all and a client
type naming a field the harness never sends would have typechecked, linted and rendered a
blank forever. It is the gap `/documents` had before #213.

`_summary_item_dict` and `_summary_totals_dict` now own that shape, and the memory and
Postgres arms both return through the first of them rather than agreeing by inspection —
those two have disagreed before, about the order of rows sharing a day. No wire change: the
keys, their types and the rounding are what they were.
