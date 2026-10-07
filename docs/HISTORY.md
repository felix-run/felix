# Felix audit-wave history

What shipped, wave by wave, and what each wave taught. This is the long-form record that used
to live under **Shipped** in [ROADMAP.md](ROADMAP.md); the roadmap is a plan again, and this is
where the plan's completed work went.

`CHANGELOG.md` records *what changed* per release. This file records *what was learned* —
including the audit conclusions that did not survive being measured, and the tests that could
not fail. Keep that habit: a wave entry that lists only wins is not worth writing down.

---

## Waves

### v0.9.0: Workers AI replaces Ollama (Oct 2026, #497)

The `ollama` provider and `FELIX_OLLAMA_BASE_URL` were removed. Open-weight models now run
on Workers AI, through the `workers_ai` provider that already existed, with six new priced
`-cf` routes. `llama-3-pro` and `llama-3-fast` stay as legacy ids that now resolve to GLM-5.3
and GLM-5.3 Flash.

- **Removing a provider took two fields with it.** Only Ollama set `bills_per_token=False`
  and `api_key_literal`. Left in place, the governance "free provider" branch would have
  checked an always-empty set: a control that looks present and does nothing. Both went, and
  the spend cap is now uniform: every route needs a price.
- **"Priced" is not "priced correctly".** The catalog matches the longest key that is a
  substring of the model id, so `@cf/zai-org/glm-5.3-flash` without its own entry still read
  as priced, at GLM-5.3's rate, about nine times too high. The cost-cap test passed. The test
  that now catches it asserts that each Workers AI route's wire id is a catalog key of its
  own, and it builds its cases from `DEFAULT_MODEL_ROUTES` rather than a hand list, which had
  already gone from 4 entries to 12.
- **A toolkit skill outlived the code it described.** `model-layer` still told the next agent
  to set `bills_per_token` on a new local provider. `validate-toolkit.py` checks paths, not
  field names, so only a reviewer reading the diff caught it.
- **Repointing an alias moves the data.** A manifest that chose `llama-3-fast` to stay on a
  local machine now sends prompts to Cloudflare once a `workers_ai` key exists. That went into
  the release notes under Security, pointing at `spec.auth.outbound.providers`.
- **The post-deploy check had been red for three releases.** The scheduled smoke against
  api.felix.run has answered 401 since 2026-10-04, and nothing blocks on it. 0.9.0 was checked
  by hand the way 0.6.1 was: all twelve bundled manifests compile in the container, and
  `oss-only` resolves to `workers_ai:@cf/zai-org/glm-5.3`. Fixing the smoke secret is on the
  roadmap.

### v0.6.1: compiling every manifest in production (Oct 2026, #451, #454–#457)

This started from a user report, "internal error (request …)" on `contributor`, and grew once we
compiled all twelve bundled manifests inside the production container. That compile costs one
script and no model calls. Three of the twelve had never compiled in production, and nothing had
said so.

- **The surface users read was the one that hid the cause.** `/chat` mapped a missing secret to
  a 503 by matching the message prefix. `/chat/stream`, which the chat UI uses, compiles inside
  its generator, after that mapping. So `secret not found: GITHUB_MCP_TOKEN` reached the browser
  as `internal error`. The fix was a type, not another prefix match: `SecretNotFoundError`, and
  `ProviderConfigError` for decision providers, both on the relay list (#451).
- **Compose dropped a setting the docs told operators to set.** `x-felix-env` passed the routes
  but not `FELIX_MODEL_PROVIDER_OPTIONS`, so every hosted provider without a settings field
  failed under Compose with "needs api_key" while `.env` held the key. We only found it because
  `printenv` in the container showed one of two lines we had just written (#454).
- **The docs, the fixture and the parser agreed, and all three were wrong.** Cloudflare's page
  for `typesafe/jev` shows one envelope. The conformance fixture copied it, and the parser
  unwrapped it. The live reply nests the answers inside a completed run one level further in, so
  correct answers (p=0.98) were reported as none (#455). Only a live call could show it, and
  that call had been an open roadmap item since the feature shipped. Before that, the live call
  returned a 402: partner models on Workers AI bill against prepaid AI Gateway credits, not usage.
  `typesafe/jev` is also missing from `wrangler ai models`, which nearly read as "the model does
  not exist".
- **A test pinned to the live file failed the release it was written for.** #456 moved
  changelog entries into PR descriptions, merged in the middle of this release, and changed step
  5 of the procedure under it. Its test asserted that the real `CHANGELOG.md` still had
  `[Unreleased]` entries. The first `cut` empties that section, so the test failed the release
  commit (fixed in #457). A test about one moment in a file's life is a test with an expiry date.
- **Moving the VM between projects surfaced two things nobody had set up on purpose.** The
  startup script reinstalled Docker on every boot and reset the daemon in the middle of a
  `compose up`. It now exits when Docker is present. And `roll.sh` takes the GCP project from
  gcloud's default, which still points at the old project, where the stopped VM sits with the
  same tunnel credentials. 0.6.1 rolled with the project set by hand, and fixing the script is
  on the roadmap.

### v0.5.0 and the first scripted production roll (Sep 2026, #363, #368, #370, #372–#375)

Not an audit wave: one release, cut and rolled to production in a day, with most of what went
wrong in the tooling around the change rather than the change.

- **A durable run waited for the minute.** Measured on the reference deployment: runs started on
  the `* * * * *` cron's :53 tick, ~25s after submission, and a sweep that stepped its batch in
  order took 89s behind one approval. A per-worker poll (1s) and concurrent steps fixed both; a
  run now starts within a second and completes a no-tool turn in 3s (#363).
- **`make up-self` could fail its own first boot.** api and worker ran `self-entrypoint.sh` at
  once on one volume; on a fresh volume both cloned and *both* failed, leaving a `.git` that
  failed the next start too. The live symptom was only the milder race on an existing clone,
  a `cannot lock ref` line per restart (#368).
- **The changelog's union merge rewrote the release twice.** Each rebase of the release PR put a
  newly merged entry under no heading and re-added a second `### Changed` and `### Fixed`. What
  held up: rebuild the close-out from `main`'s changelog mechanically and assert every entry line
  survives, rather than resolving the merge by eye. And merge a release PR the moment it is green.
- **Gates run before the bump miss what the bump breaks.** `test_version_single_source` built its
  expected file with `replace(version)`, which at 0.5.0 also rewrote `pgvector>=0.5.0`; the bump
  was right and the oracle was wrong, and only a release number matching a pin could show it.
- **A CVE landed mid-release** (PyJWT, #372). The fixed version was 18 days old and the newest
  was inside the 48-hour hold, so the lock pins the fix rather than the latest.
- **The roll script's dry runs passed with stubs that behaved better than the real tools.** Its
  first real run hung at `gh`'s pager, lost typed-ahead answers to `gcloud compute ssh`, and
  half-switched `/opt/felix` because earlier `sudo git` rolls left 485 root-owned paths (#374).
  The fix's tests use stubs that page and swallow stdin, and the old script fails them. A stub
  is only evidence when it can fail the way the real thing does.
- **The smoke test was a source of the noise it should detect.** Its durable prompt made `cowork`
  write a file, so every six hours it left a pending approval in production's queue (#375).
  Approval rows store `created_at` in milliseconds; reading them as seconds dated that row to
  06:08 and nearly misattributed it.

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
