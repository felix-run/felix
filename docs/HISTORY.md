# Felix audit-wave history

What shipped, wave by wave, and what each wave taught. This is the long-form record that used
to live under **Shipped** in [ROADMAP.md](ROADMAP.md); the roadmap is a plan again, and this is
where the plan's completed work went.

`CHANGELOG.md` records *what changed* per release. This file records *what was learned* —
including the audit conclusions that did not survive being measured, and the tests that could
not fail. Keep that habit: a wave entry that lists only wins is not worth writing down.

---

## Waves

### Governance mutation audit (Sep 2026, #141–#150)

Method: disable each of the nine governance controls in turn — `return tools`, the shape a
control takes when it is silently absent — and re-run the whole suite against each. Seven were
noticed by tests written for them; two were not. Everything below came from that, or from the
security reviews of the fixes.

- **`replay_safe` had never worked in any release.** Seven wrappers and `wrap_tool` rebuilt the
  tool from eight of its ten fields; `apply_limits` wraps every tool unconditionally, so the
  flag read `False` on every tool in every manifest and `patterns/react.py`'s "safe to call
  again" branch had never executed (#141).
- **A `spec.policies` rule with `tools` and no `required_scopes` permitted everyone** while
  appearing governed — the tool *was* wrapped, so the compiled stack looked correct (#142).
- **Glob tool targeting**, which the docs had promised for months and no control implemented,
  across all five tool-targeting lists. Approvals needed literal-beats-pattern precedence to
  stay non-weakening (#143).
- **One model tool call could execute a tool twice** — both dispatch sites probed arity by
  calling and catching `TypeError`, which cannot tell wrong arity from a `TypeError` raised
  inside a body that already ran (#144).
- **Post-call bookkeeping told the model a successful tool call had failed**, and ran the
  after-tool hook twice (#145).
- **`content_screening.tools` was substitutive**, so naming one trusted tool silently unscreened
  every MCP, peer, browser, sandbox and queue tool (#146).
- **Policies nothing in the configuration can satisfy** are named at compile (#147).
- **Untrusted tools bound with screening off** are named at compile; found `cowork.yaml` running
  `local_shell` on the user's machine unscreened (#148).
- **A durable run resumes as the caller who started it**, bounded three ways: never wider than
  the caller, never longer than the run (TTL now capped), never longer than the token's `exp`
  (#149).
- **The fiber lease equalled the approval timeout**, so a run parked on a decision was
  re-claimed and re-executed with side effects already committed; and manifest resolution ran
  outside the tenant context, so under RLS a durable resume could execute the *bundled*
  manifest (#150).

Also deleted: `scripts/prove-fails.sh` and `.claude/hooks/structural-test-proof.sh`. Measured —
the two PRs that built them changed zero production files and their tests were 28% of suite
runtime. The method survived the tools; the `test-quality` skill describes mutation directly.

The recurring failure in the *fixes*, worth remembering: testing the helper instead of the call
site. It happened five or six times, and a mutation is only evidence when the test **fails** —
two runs reported red with zero failed tests, which were collection errors.


### RLS opt-out coherence (Aug 2026, v0.2.1)

`0006_tenant_rls` applies `ENABLE` *and* `FORCE ROW LEVEL SECURITY`
unconditionally, but the application set neither GUC unless `FELIX_DATABASE_RLS`
was true — which it is not by default. On any connection RLS actually applies to,
all 16 tenant tables returned zero rows and rejected writes, silently. Only a
superuser or `BYPASSRLS` role escaped it, which is what the bundled compose stack
uses, so it never appeared locally while being a total outage on managed
Postgres. The listener now declares `app.rls_bypass` when RLS is off, making the
flag a real runtime toggle; the migration stays unconditional so the schema is
reproducible. `felix doctor` reports coherence between the two halves, including
the case where RLS is on but the connection skips the policies entirely.

`docs/UPGRADING.md` also landed: the upgrade path had lived in whoever last did
one, and `RELEASING.md` stops at the tag by design.

### Connections and notifications (Aug 2026)

Two ceilings the ASGI audit had measured but not removed: connections, and query
volume that grows with connected clients rather than with work. Landed as #91–#94
and #96–#99, plus #101. Then a review pass over the result, which is where most of
this list came from.

- [x] **A pooler seam, and then the pooler.** `FELIX_DB_PREPARED_STATEMENTS`
      (#91) exists because psycopg3 auto-prepares after five executions and the
      sixth lands on a different server connection under transaction pooling —
      five identical queries succeed first, so the symptom arrives detached from
      its cause. `make up-pooled` (#94) makes PgBouncer a target rather than a
      paragraph. Booting it (#96) is the first time anything pulled the image,
      and the pull failed: `edoburu/pgbouncer:1.25.2` is the version PgBouncer
      prints in its own log, not a tag. A test asserting "pinned and not
      `:latest`" was happy with a tag that resolves to nothing. Once fixed, the
      overlay did what it claims — api, worker and scheduler, each with its own
      pool, sharing **two** Postgres backends, and 40 consecutive requests past
      the prepare threshold with no error.
- [x] **Wake a resume stream instead of asking it every second** (#93) — query
      volume there grew with *connected users*, not with turns, which is the
      line that crosses first at scale. Redis pub/sub, ref-counted on one shared
      subscriber connection, with the poll left underneath: the notification is
      a hint, never the source of truth, so a dropped message costs latency and
      never correctness.
- [x] **What shipping that broke, found by running the quality reviewers
      retroactively** (#97). Worth recording because none of it was caught by
      the tests that shipped alongside it:
      - `_announce` defaulted `tenant_id` to `"default"` and the in-memory
        session had no tenant to pass, so every `memory://` append announced on
        that channel whoever wrote it. A real tenant's reader was never woken; a
        `"default"` reader was woken by other tenants' writes. `get_session_store`
        had the same bug in its storage half. Every test used `"default"` — the
        one value that could not fail. **Third instance, Aug 2026:**
        `memory/tools.py:_provenance` called `get_session_store(settings)` with no
        tenant, so `remember` read tenant `"default"`'s log for every caller and
        stamped `origin_seq = 0`. Same shape, same cause: a `tenant_id` that
        defaults. The defaults are now gone from the session accessors and
        `tests/unit/test_invariants.py` fails if one comes back — a required
        keyword catches omission, though not an explicit `tenant_id="default"`.
      - The subscription was scoped to a *wait*, not a reader, so a stream
        subscribed and unsubscribed once per poll interval while the docstring
        said "as readers come and go".
      - The refcount was taken *after* the SUBSCRIBE round trip, so a departing
        reader could unsubscribe a channel an arriving one was still waiting on
        — while it reported `by_notification=True` and stretched its poll to a
        minute. A stream that believes it is being woken and is not.
- [x] **A spent connect guard latched notifications off for good** (#101) —
      found by reviewing a CodeQL false positive rather than by the alert being
      right. `_connecting` is a single-flight guard whose `finally` does not run
      if the loop closes mid-connect; every later call then short-circuited on it
      and returned `None` without attempting a connect, for the life of the
      process.
- [x] **Collapsed `_connect_args` back into `_pool_kwargs`** (#98). The split
      reintroduced exactly the shape `_pool_kwargs`'s docstring exists to
      prevent, and the AST test asserting both builders passed `connect_args`
      existed only because they could diverge. Also added the conformance arm
      that runs seven queries against a real connection — that the setting
      reaches the driver was a pure-function assertion; that it stops psycopg
      preparing had been checked once, by hand.
- [x] **Separated resume pacing from resume framing** (#99) — 107 lines to 83.
      The point was not the line count: the 60-second notified ceiling, the whole
      reason #93 exists, shipped a release with no assertion anywhere, and the
      reason was visible in what covering it took. It is four lines now.

- [x] **Proved the thing two replicas do that one cannot** (#104) — `notify.py`'s
      whole reason for existing was the part with no coverage, because in one process
      the in-process waiter answers first and Redis is never consulted. Building the
      proof also found that the CI docker job validated only the base compose file:
      the overlays were excluded from `check-yaml` for using `!reset`, under a comment
      promising CI checked them instead, and CI did not. Turning that on found the gcp
      overlay could not be parsed at all without two variables nothing had ever
      supplied.

Three tests written during this wave did not fail against the code they were
written for, and were only caught by running them against it: a race repro whose
fake let two SUBSCRIBEs overlap when a real connection serializes them, a
Makefile check that matched a variable definition rather than the recipe, and a
grace-window assertion that was simply off by one. The habit that catches these
is running a new test against the unfixed code and requiring FAILED, not ERROR.

### ASGI latency audit (Aug 2026)

A measured audit of the FastAPI/Starlette layer, then nine of its ten findings.
Every figure below came from a benchmark against this checkout rather than from
reading the code, and three of the audit's own conclusions did not survive being
measured.

- [x] **Four `BaseHTTPMiddleware` layers wrapped every request and every
      streamed token.** Starlette implements each with a task group, an
      `anyio.Event` and a zero-buffer memory object stream, so a response chunk
      crossed four of them. Converting all four to pure ASGI took `/health` from
      651.6 µs to 125.3 µs and an SSE chunk from 77.6 µs to 1.5 µs. The
      streaming body cap became real in the same change: `call_next` ignores its
      `request` argument, so the capped receive channel was never read and a
      chunked upload with no `Content-Length` had no limit at all (#55)
- [x] **`with_heartbeat` allocated a task per streamed event** — 44.17 µs to
      1.10 µs with a pump task and a bounded queue (#55)
- [x] **The pool was hardcoded at 5 + 10 in two places**, so fifteen
      connections per worker was a ceiling nobody could raise; `FELIX_WORKERS`
      was a bare `os.environ` read. Both are settings now, and both engines size
      themselves through one function so they cannot drift apart again (#66)
- [x] **`append_batch` discarded the sequence numbers it had just allocated
      under the lock** — returned now, asserted on both arms (#67)
- [x] **Rate-limit eviction ran `max(v)` across every tracked key**, and keys
      are per-IP, so the defensive component's cost grew with the attack it
      absorbs: 1412.7 µs to 6.7 µs at 50k keys. The bigger everyday win was the
      steady-state hit — 13.92 µs to 0.37 µs — which the audit had measured at
      1.5 µs and explicitly ruled out (#68)
- [x] **The bundled skills catalog was re-walked on every chat request**,
      synchronously on the event loop: 56.6 µs to 1.4 µs. The audit's suggested
      mtime key would have bought almost nothing — the walk dominates, not the
      reads (#69)
- [x] **Five sequential store reads on the reattach path** — 2.66 ms to 1.38 ms
      against a real Postgres, and the gap widens with network latency rather
      than narrowing (#70)
- [x] **The resume stream polled at a fixed 1 Hz per client until 300 s of
      silence** — 300 polls per idle window down to 61, with the first thirty
      seconds deliberately left at the floor so reattach latency is unchanged
      (#71)
- [x] **Credentials were re-parsed on every authenticated request** — 21.5 µs
      to 0.4 µs. The audit's hashed-index suggestion was implemented and then
      dropped: it optimised the wrong half, and CodeQL was right that a hash of
      a credential in the auth path needs an argument nobody should have to
      make (#72)
- [x] **Four resolver caches were unbounded and three were tenant-keyed**, so
      every tenant that resolved a manifest left an entry for the life of the
      process (#73)
- [x] **`GET /chat/history` returned every message a thread had ever had.**
      Bounded and pageable, taking the newest window rather than the oldest —
      `get_events(limit=n)` takes the first n, which for a transcript is the
      wrong end (#74)

### Cross-harness port audit (Aug 2026)

Second pass against a sibling runtime, this time looking for features to port.
Like the first, it mostly found bugs — in code that two sessions had each half
fixed, and in a delivery path nothing asserted end to end.

- [x] **`memory_vectors` rejected every insert on Postgres.** The column
      `embedding vector(768) NOT NULL` has existed since `0001_baseline` — added
      by raw SQL, so invisible from `db/models.py` — and `put_memory` never
      supplied a vector. Every insert raised NotNullViolation, and the only
      caller swallowed it into a debug log, so long-term memory had never stored
      a row outside the in-memory twin. Found by the first Postgres conformance
      test the store ever had (#46)
- [x] **Migrations were never executed by CI.** The `conformance` job runs a real
      pgvector Postgres (#39), but built its schema with `Base.metadata.create_all`
      — so no revision ran, and the DDL that lives only in a migration was never
      present. `create_all` cannot produce a generated column or a GIN index, which
      is exactly what the memory and audit work depends on. The Postgres arm now
      applies `alembic upgrade head`, and three tests assert every revision applies,
      reverses, and produces `session_events.content_tsv` and its index (#45)

- [x] **Recalled memory facts never reached the model on a threaded chat.**
      `_assemble_messages` built the list with the prelude, then let the session
      strategy replace it wholesale. Four existing tests asserted the block was
      *built* correctly; none that it survived assembly (#43)
- [x] **Embeddings ran on the event loop**, stalling every concurrent request on
      the worker — tool retrieval reaches the encoder up to four times per loop
      step. Threaded, with the cheap keyword path kept inline and a guard pinned
      to the code it mirrors by test (#44)

The port items themselves are under **Next → Harness → From the cross-harness
port audit**. The streaming double-inference this pass also flagged was already
fixed by `#36`.

### Harness audit wave (Aug 2026)

Cross-harness audit that set out to find portable packages and mostly found
bugs — fifteen of them, clustered almost entirely in code that existed in two
copies with nothing comparing them, or in code nothing exercised.

- [x] Truncated turns executed their tool calls, past command screening —
      arguments can be cut off mid-write and still parse (#34)
