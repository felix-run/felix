**A caller's `taskId` became a thread id with none of the checks a client's suffix
passes.** `a2a/server.py` built `f"{tenant_id}:a2a:{task_id}"` straight from the A2A
`message/send` params, and `eval/runner.py` built `f"{tenant_id}:eval:{run}:{item_id}"`
from dataset items written through `PUT /eval/datasets/{name}`. Both skipped the rules
`effective_thread_id` applies to every thread id a client names: the `#` rejection and
`MAX_THREAD_ID`.

The cap is not cosmetic. `thread_id` is the tail of the `session_events` primary key and
`task_id` is half of the `a2a_tasks` one, both plain btree indexes, so an incompressible
id past roughly 2700 bytes does not bloat the index — it fails the insert:

```
ERROR: index row size 3864 exceeds btree version 4 maximum 2704 for index "..._pkey"
HINT:  Values larger than 1/3 of a buffer page cannot be indexed.
```

That is a 500 an authenticated caller can repeat at whatever the rate limit allows. A `#`
was quieter and just as wrong: `thread_belongs_to_tenant` rejects `#`, so the server minted
a thread of its own that `/internal` then refused and no operator could address. Neither
shows up on `memory://`, which keys a dict.

Both call sites now compose through a named helper in `felix/thread_ids.py` —
`a2a_thread_id` and `eval_thread_id` — which apply the same rules and return nothing when
they fail. A2A answers `-32602` **before** writing the task row; an eval item with an
unusable id fails that item rather than the run, matching the rubric check beside it.

One composer per namespace rather than one variadic helper, so *which* segment may carry a
`:` is fixed by a signature rather than by how many arguments a call site happens to pass.

`:` stays legal in that segment, so the `urn:uuid:…` task ids several A2A clients send keep
working — it is the whole remainder of the id, so the split is unambiguous however many
separators it carries. Composed names are otherwise byte-identical to before, so no
existing thread moves.

`felix_api/threads.py` moved to `felix/thread_ids.py` to make this possible: the harness
mints thread ids and cannot import the app, and the rule is one the module's own docstring
says lives in a single place.
