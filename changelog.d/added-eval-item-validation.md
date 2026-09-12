**Eval dataset items are validated instead of silently stored empty.**
`PUT /eval/datasets/{name}` and `felix eval --fixture` now refuse an item that would be
stored and then score nothing, and name what to change: a prompt under a near-miss key
(`input`, `prompt`, `question`, …) rather than `user_input`, a rubric that is not an object,
or a repeated `item_id`. Every problem in a batch is reported at once, and nothing is written
when the batch is refused. The rubric itself stays free-form.

A rubric naming none of `expect` / `equals` / `contains` / `min_chars` is legal — it scores as
`nonempty`, which passes any answer that is not blank — so it lands with a **warning** rather
than a refusal: the route returns it alongside the stored dataset, the CLI prints it to stderr.
That is the case where a dataset looks configured and gates nothing.

The CLI exits **2** for a malformed fixture, distinct from the exit 1 that means the eval ran
and items failed, which is what `scripts/eval-counter-smoke.sh` and the CI eval job read.

The refusal body is `{"code": "eval_items_invalid", "errors": [...], "warnings": [...]}`. The
code matters because this endpoint returns 422 twice over — a body failing the request model's
`extra="forbid"` gets pydantic's own list-shaped `detail` — so a client can tell them apart
without type-sniffing. A successful `PUT` always carries a `warnings` array, empty when there is
nothing to say.

Behaviour change: a `PUT` that previously returned 200 for an unrecognised item now returns
422. `tests/e2e/test_mgmt_routes.py` pinned the old behaviour and said in its own docstring
that it should fail when validation arrived; it now pins the refusal.
