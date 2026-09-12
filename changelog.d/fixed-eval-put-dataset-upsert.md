**The second run of any eval failed on Postgres, and the canary monitor stopped scoring after
its first ever tick.** `put_dataset` is the only way an eval item is written and every caller
repeats item ids by design, but it did a plain `db.add` per item — so writing an item id that
already existed raised `UniqueViolation` on `(tenant_id, dataset_name, item_id)`. The in-memory
twin overwrote happily, so the entire suite was green and only a real deployment failed. It is
an upsert now, updating `user_input` and `rubric_json` and leaving `created_at` at the item's
first appearance; the twin was aligned to keep the same `created_at`.

What it was breaking, measured against a live Postgres rather than inferred: `felix eval
--fixture <file>` succeeded once and 500'd on every later run of the same file, as did
`PUT /eval/datasets/{name}` with any repeated item id. Worse, the scheduled `continuous_eval`
sweep re-puts its sampled dataset on every 10-minute tick, and `run_continuous_eval_all_tenants`
logs and swallows a per-tenant exception — so from the second tick onward it scored nothing and
returned `{"runs": 0, "tenants": 1}`, a success-shaped result. Three consecutive ticks now
report one run each; before the fix they reported 1, 0, 0.

`tests/conformance/test_eval_store.py` is the new arm that holds the two backends to one
contract, which is what would have caught this: the eval store was on the roadmap's list of
stores with no Postgres arm.
