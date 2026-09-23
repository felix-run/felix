**An eval `item_id` that cannot become a thread is refused at the write, not at the run.**
0.3.0 stopped an A2A `taskId` and an eval dataset `item_id` from reaching a thread id
unchecked, but closed the eval half only where the run composes the thread. `PUT
/eval/datasets/{name}` puts `item_id` straight into the `eval_dataset_items` primary key —
three `Text` columns, plain btree — so the same insert failure sat one route *earlier* than
the guard that shipped, before `eval_thread_id` ever saw the value:

```
ERROR: index row size 3864 exceeds btree version 4 maximum 2704 for index "..._pkey"
HINT:  Values larger than 1/3 of a buffer page cannot be indexed.
```

`validate_items` refuses it now, which is that module's stated remit: an item stored with
such an id was accepted with a `200` and then failed *every* run forever, with the reason
buried in `scores`.

**Upgrade note.** This is a new `422` on a route that accepted these ids in 0.3.0 and
earlier. The limit is `MAX_EVAL_ITEM_ID` (345 characters) and it deliberately does **not**
depend on the tenant — deriving it from whoever stores the dataset would make one file valid
in one deployment and refused in another. An id over the limit was already unrunnable, so
what changes is where the author finds out, not whether it works.

Two smaller fixes alongside it, both from the same review:

* `eval_item_failed` interpolated `item_id` into a log line unescaped. Pre-existing, but the
  refusal above is a new deterministic way to reach it with an id chosen to be malformed, so
  it goes through `loggable()` like every other untrusted value in a log line.
* An A2A `taskId` was `str()`-coerced before being checked, so a JSON object became a thread
  id from its Python repr and the guard validated that rather than what the caller sent. It
  is refused with `-32602` now. Harmless in practice — same tenant, still injective — but it
  is the distinction the rest of that path is careful about.
