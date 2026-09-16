**`deploy/GOVERNANCE.md` told operators that `spec.policies` and `execution.mode: durable`
could not be combined.** They can, and have been able to since fibers began recording the
caller's authority. One bullet said durable fibers "carry an empty scope set" and that the two
features are "therefore not usable together today — every policied tool denies"; a bullet four
lines below it said "Durable runs are the exception … so `spec.policies` and `execution.mode:
durable` work together", and a paragraph further down described the recorded-authority model in
full. The first was stale and the rest were current, and a reader hitting the stale one first
would abandon a configuration that works.

Corrected against the code rather than reconciled by preference: `start_durable_chat` writes
`state["auth"]["scopes"]` from the caller (`durability/runs.py`) and the resume rebuilds an
`AuthContext` from it (`durability/fibers.py`). The genuinely scopeless cases are named and
verified — `auth_mode=none` (the middleware returns `ANONYMOUS`), scheduled jobs (principal
`cron`) and `felix eval` (principal `eval`), the last two because they construct an
`AuthContext` with no `scopes` argument and take the field default. A fiber with **no recorded
caller** — enqueued outside a request context, or written before fibers carried authority —
still denies, which is the fail-closed direction and is what the stale sentence originally
described before it outlived its scope.

The same claim had been copied into the public docs (`internals/governance.mdx`) and is fixed
there in `felix-run/web#172`. It appears nowhere else in this repo.
