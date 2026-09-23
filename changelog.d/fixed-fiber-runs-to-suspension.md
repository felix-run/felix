**A durable run no longer spends a scheduler tick per step, or a whole tick noticing it had
finished.** `resume_due_fibers` stepped each claimed fiber exactly once, and `_run_fiber_step`
only flips a fiber to `completed` on the sweep *after* the one that ran its final step, when
it notices `cursor >= len(steps)`. A durable chat's `steps` has length one, so the cheapest
possible durable run took two `* * * * *` ticks — around two minutes, the second of which did
no work at all. A *failure* terminates inside one sweep, so a failed run reached its terminal
state a full minute before a successful one.

A claim now runs the fiber to its next suspension. Measured on both the old and new code:

| fiber | before | after |
|---|---|---|
| durable chat (one `invoke`) | 2 ticks | **1** |
| stash → complete | 2 | **1** |
| three stashes | 4 | **1** |
| `invoke` → stash | 3 | **1** |
| two `invoke`s | 3 | **2** |
| `sleep` → complete | 2 | 2 |

**Fairness is unchanged**, which is the bound that makes this safe: a claim runs at most one
`invoke`, the only op that can take seconds, so wall-clock per fiber per sweep is what it
always was. What goes away is the ticks that were doing bookkeeping. A `sleep` still ends the
claim — the loop runs *to* suspension, so a fiber asking to wake in an hour still does.
`FIBER_MAX_OPS_PER_CLAIM` (64) and a cursor-advance check bound a pathological `steps` list.

Three supporting changes, all of which would have been silent defects:

* A save that loses its compare-and-set is reported by `_save_fiber` as a log line, not an
  error, and `_run_fiber_step` has already mutated its row in place — so `status` and
  `cursor` still read as progress. One step per claim made that self-limiting; a loop would
  run on for up to `FIBER_MAX_OPS_PER_CLAIM` more ops against a row that now belongs to
  another worker, including its one `invoke`. The loop checks whether its write landed and
  yields the claim if it did not.
* The failure path keeps its claim until `_retry_or_dead` parks the fiber. Releasing first
  left the row `status="running"` with a null `lease_until` between two transactions, which
  is exactly what the claim query selects — a concurrent sweep would re-run the step that
  just failed.

* The claim is held across the whole loop and released once at the end. Releasing per step
  and re-acquiring is not equivalent — `_renew_lease` only renews a lease this worker still
  holds, so the gap would let a second worker claim the fiber and run the next `invoke`
  concurrently, which is a duplicated side effect rather than a lost write. `_release_fiber`
  is now scoped to this worker's own claim for the same reason.
* `attempts` counts *consecutive* failures, and a landed step and a failed one can now share
  a claim. A failure after progress is charged as the first of a new streak, so a fiber that
  had just advanced is not buried by a stale count.
