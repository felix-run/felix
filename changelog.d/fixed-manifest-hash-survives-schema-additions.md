**Adding a field to the manifest schema no longer breaks every pinned thread and every
in-flight durable fiber.** `manifest_content_hash` dumped the model with every field the
schema declares, so a field *added* to `spec` — with a default, changing nothing about how
any manifest compiles — moved the content hash of every manifest already stored.

The consequences were an outage nobody asked for, and fail-closed rather than silent: a
thread pinned under `governance.pin_compile: true` raised `ManifestDriftError` on its next
turn with nothing in its own text having changed, and every in-flight durable fiber failed
at resume, because `durability/fibers.py` forces pinning for any fiber carrying stored auth
regardless of the manifest's own setting. `spec.skills_declared_only` did exactly this one
release ago, which is why it shipped with an upgrade note telling operators to drain fibers.

The hash now excludes fields sitting at their default. For a *manifest* edit that changes
nothing — a manifest writing `pin_compile: false` and one omitting it compile to the same
agent, and the old dump already hashed those two identically. Drift detection is unchanged
and now asserted in both directions, which is the half worth naming: moving a field off its
default adds a key and moving it back removes one, so both are still seen. A hash that
noticed only additions would let a pinned thread keep running after its governance was
switched off — that is a test, not a hope.

**What it gives up, stated rather than glossed:** a *release* that changes what a default
means. Shipping a new default for a governance field used to move every stored manifest that
omitted it, and the pin fired; now it does not, and those manifests compile differently while
hashing the same. **Changing a default is therefore a migration** — rewrite the rows or
rotate the pins — joining the family `manifests/compat.py` already names alongside removing a
key and narrowing a field. The alternative, a second digest over the schema's own defaults,
would move on every field addition and reintroduce exactly the outage this removes.

**Upgrading.** This rotates every content hash exactly once, so the release carries the same
one-time cost as the additions it prevents — taken deliberately, once, instead of
accidentally on the next schema change and every one after. Two details the obvious reading
misses:

- **Threads that were never pinned are affected too.** Every thread gets a hash
  soft-recorded on first touch with `pin_compile=False`, and the check enforces when *either*
  side asks for it — so a thread whose manifest later turns pinning on refuses once, with
  nothing having changed.
- **A drifted pinned thread stays refused, not refused once.** There is deliberately no
  tenant-facing pin reset. The recovery is `POST /fork`, which starts a thread with no pin
  and keeps the history; durable fibers are marked `failed` with the drift text and are not
  retried, so there is no storm — they are re-enqueued, or drained before the deploy.
