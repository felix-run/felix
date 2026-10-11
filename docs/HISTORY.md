# Felix audit-wave history

What shipped, wave by wave, and what each wave taught. This is the long-form record that used
to live under **Shipped** in [ROADMAP.md](ROADMAP.md); the roadmap is a plan again, and this is
where the plan's completed work went.

`CHANGELOG.md` records *what changed* per release. This file records *what was learned* —
including the audit conclusions that did not survive being measured, and the tests that could
not fail. Keep that habit: a wave entry that lists only wins is not worth writing down.

---

## Waves

### Roadmap tidy: completed items folded in (Oct 2026)

Moved here verbatim from `ROADMAP.md`, which had reached 2,100 lines, half of them `[x]` items.
Each item keeps its own account of what was found and fixed. They are grouped under the roadmap
section they were filed in. Items still open, and done children of an open parent, stayed
behind. The lesson is the roadmap's own legend: "fold into HISTORY.md on the next tidy pass"
only works if a pass happens, and none had for about 110 items.

#### A. Capability surface

- [x] **`http_fetch`** — `spec.http_tools` binds a fetch tool per ref; `support` uses it as
      `fetch_docs`, confined to the docs site. Two corrections to this entry as written, both
      found by reading the code rather than the note: the existing `HttpExecutor` was the wrong
      starting point (it posts tool *arguments* to a manifest-fixed URL — operator picks the
      destination; here the model does, which is the whole risk), and the "pin the connection"
      prerequisite was **already met** by `#128`–`#130`, so `safe_async_client` gave it for free
      including on redirect hops. `http` stays out of `_TRUSTED_TRANSPORTS` and was added to
      `_UNTRUSTED_SOURCE_PREFIXES`; both layers are pinned separately, because asserting only
      their combination left either free to regress.

- [x] **Web search** — `felix/search.py` carries the `SearchBackend` Protocol and
      `register_search_backend`, selected by `FELIX_SEARCH_BACKEND` and validated against the
      registry at boot. `spec.search_tools` binds the tool. One correction to this entry: the
      bundled backend needed **no extra**, because SearXNG speaks JSON over httpx, which is
      already core — the extra was assumed rather than checked. SearXNG rather than a hosted
      API because it is the one an operator can run themselves without an account, which is the
      same argument as `FELIX_OBJECT_STORE=fs`.

- [x] **Structured output** — `spec.output_schema` is a JSON Schema the answer must match, and
      `/v1/chat/completions` accepts OpenAI's `response_format` for the same thing per request
      (the manifest's wins). The OpenAI wire emits `response_format`, strict when the schema
      closes every object and requires every property; the Anthropic wire, which has no
      equivalent on older models, sends the schema as a tool the model must call and folds the
      call back into the reply, so `message.content` is a JSON document on either. `tool_choice`
      is `any` rather than naming that tool whenever real tools are also bound, so a react loop
      can still reach them. A model with native structured outputs (`ModelQuirks.structured_outputs`)
      gets `output_config.format` instead when the schema is inside Anthropic's subset
      (`output_schema.anthropic_native_misfit`), merged beside `effort`; and a model that refuses a
      forced choice (`ModelQuirks.forced_tool_choice` — Fable 5.1, Mythos 5.1, Opus 5.5,
      Sonnet 5.5, and every family key and unknown id) is never sent `any` or `tool`.
      - **The composite patterns** — done for four of five. `router`, `parallel`, `reflect`
        and `plan_execute` now thread `ModelChatOptions` onto the one turn whose output the
        caller receives, and declare `honours_output_schema`. Placement is per pattern and
        explicit at each call site: the parallel synthesis but not its specialists, the
        plan_execute synthesis but not the plan or the steps — which needed the schema
        stripped from the context its executor is built from, since `build_react_agent` reads
        it onto the agent itself — the routed child but not the classifier, and every reflect
        draft because the loop exits early. The manifest outranks a request's
        `response_format`, matching react. `_child_input` no longer
        drops `model_options`, so a caller's `/v1` `response_format` reaches a child too.
        `groupchat` stays refused with the reason recorded next to its registration: its answer
        is the last speaker's message stamped `[name] …`, so a child's JSON comes back with a
        prefix on it. Supporting it means dropping the stamp or adding a synthesis turn.
      - Not done, and deliberately a separate item: **validation with a repair retry.** The
        provider is what enforces the shape here, which is the guarantee worth having and is why
        this shipped without a retry loop. Native structured outputs closed the extended-thinking
        hole for every model that has them and every schema inside their subset. What is left is
        offered, not guaranteed, and logged with the reason: a schema outside the subset
        (`maxLength`, `minimum`, `pattern`, `minItems`, recursion, an open object) on a model that
        refuses forcing or with thinking on; Sonnet 4.x and Opus 4.6/4.7 with thinking on; and an
        id the catalog does not know. A validate-and-retry pass would close those arms; until then,
        do not promise the shape there. Native output on a `refusal` or `max_tokens` stop may not
        match the schema either — the stop reason passes through, as it does on the tool route.

- [x] **Make the bundled manifests use them.** `support` fetches from the docs site and now
      searches the corpus as `search_docs`; `deep` has `search` + `fetch`. Both with screening
      on, which is what keeps the unscreened-tools warning silent on what we ship. A tool no
      manifest declares is inert by this repo's own definition, and none of these are now.

- [x] **Governed shell tool.** The decision gate that sat here — the `read`/`edit`/`bash` coding
      toolset, deferred as "only worth starting if coding-agent use cases are actually on the
      roadmap" — is decided: [SELF.md](SELF.md) puts Felix building Felix on the roadmap,
      and rung 2 of it cannot exist without a way to run `./scripts/test.sh`. Landing as
      `spec.shell_tools` behind `FELIX_SHELL_ALLOWED_COMMANDS` (argv prefixes, no shell interpreter,
      scrubbed env, cwd pinned under the workspace root), not as a `ShellBackend` registry — one
      implementation does not earn a registry. The local child could read the API's environment
      through `/proc`; `FELIX_SHELL_RUNNER_URL` now sends the exec to `felix-shell-runner`, which
      the builder stack runs as a `shell` container holding no secrets. Shipped: the tool, the
      allowlist, and the runner (#397); `contributor` declares it.
      Open, and a deployment choice rather than unfinished work: local exec is still the default
      everywhere but the builder stack, so a shell tool there runs beside the API's environment
      unless `FELIX_SHELL_RUNNER_URL` is set.

- [x] **Decision models (Jev).** Plan: `~/.claude/plans/we-want-to-use-sprightly-sprout.md`.
      Some calls decide rather than write, and each one asked a chat model for prose and
      parsed it. Examples: the router's "reply with only the agent name", which silently
      falls back to the first sub-agent; the judges' `{score}` JSON; the injection scorer's
      "number only". The seam is `felix_ai.decide` plus `FELIX_DECISION_ROUTES`, with the
      `typesafe`, `workers_ai` and `llm` backends. Each consumer opts in on its own, and keeps
      its current path as the fallback when the decider errors or is not confident enough.
      Land in order, one PR each:
      1. [x] The seam, plus `tools_retrieval.decider` (#314).
      2. [x] The router's `_choose_child`, and confidence escalation's `_low_confidence` (#315).
      3. [x] (#318) Judges, reply judges, eval `llm_judge`, and the reflect verifier: a `Noul` per
         criterion.
      4. [x] (#321) Skill suggestion: the two-stage rank-then-rerank, emitted as a transient prompt hint.
      5. [x] Injection screening as a `Noul` battery. It is *additive* to the regex and the
         LLM scorer, because Jev is not adversarially robust. Needs a security review.

      Verified live on 2026-10-03, and the check was needed: Workers AI returns the answers inside a
      completed run, one level deeper than the envelope the model page documents and the fixture
      encoded, so every call was read as carrying no answers (#455, in 0.6.1). Production now routes
      `jev` to Workers AI through the `felix-prod` AI Gateway. Partner models bill against prepaid,
      account-level AI Gateway credits (a 402, code 2021, when empty), not Workers AI usage.

- [x] **Clef as the default decider.** Cloudflare's Clef and Clef-flash (2026-10-01) are
      Jev-API compatible, open-weight, 64K context, and billed as Workers AI usage on the credential
      the chat routes already hold. `clef` and `clef-flash` are now default decision routes and
      `decider-support` uses `clef`; the `jev` routes stay. A `@cf/` model is run by path
      (`/ai/run/@cf/cloudflare/clef`) with flat fields, unlike Jev's nested partner run. Verified
      live on 2026-10-06 from the production container through the `felix-prod` AI Gateway: both
      models answered every question type in a single `{result: {model, answers, usage}}`
      envelope, about 0.6–0.75 s per call including connect. Follow-up, not wired: Clef reads images (`images`,
      up to 4), which `image_screening` could use instead of transcribing first.

- [x] **Sub-agents are compiled from bundled YAML only.** Found in a real run of the router
      e2e test: `runtime.py:build_tenant_agent` never sets `BuildDeps.sub_agent_builder`, so
      `builder.py` compiles each `spec.sub_agents` name with `build_agent(name)` →
      `load_bundled(name)`, and a name missing from `manifests/` becomes an *empty* manifest
      (`You are <name>.`, no tools). A router whose children live in the manifest store — the
      tenant's own agents — routes to blank agents without an error. Fix: resolve children
      through `resolve_tenant_manifest` with the parent's tenant, and fail the compile on a
      name that resolves nowhere.

      Two decisions this surfaced, both taken:
      - [x] **Child inbound auth — inherited.** `enforce_inbound_auth` has never run on sub-agents — bundled
        `router` (`allow_anonymous: true`) already reaches `deep` (`allow_anonymous: false`).
        With tenant-authored children, an author can no longer rely on a child's
        `required_scopes` once it sits behind a public router. Enforce at compile (a child
        stricter than its parent fails — which breaks bundled `router` → `deep` as written), or
        document that routing inherits the parent's auth. Decided: inherited, documented in
        `deploy/GOVERNANCE.md` under inbound constraints.
      - [x] **Shallow compile pins — now deep.** `ensure_thread_pin` and `assert_pin_matches` hash the parent
        only. Children used to change only with a deploy; stored children change mid-thread,
        so a `pin_compile` thread picks up an edited child's tools and policies. Fold
        `(child, version, hash)` into the pin, or say in `deploy/GOVERNANCE.md` that pins are
        shallow. Done: a `sub_agents_hash` beside the parent's, checked on every turn and
        durable resume; pins taken before it gain the digest on their next turn.
      - [x] **Children are resolved twice per turn.** The pin digest resolves them in
        `ensure_thread_pin`, the compile again in `runtime._tenant_sub_agent_builder`, so a child
        activated between the two (a 30s active-pointer expiry) compiles for one turn under a pin
        that checked its predecessor. Closing it means handing the checked tree to the build —
        `prepare_tenant_invoke` returning what it resolved, through its ~10 callers. A
        request-scoped cache is the tempting shortcut and the wrong one: worker tasks run
        fibers back to back, and a stale entry would compile the wrong child.

- [x] **Skill authoring & library** — Felix can list and activate Agent Skills but nothing
      can write one. Agents draft, operators review and publish, and chat-ui gets a library,
      editor, diff and review queue; Skillist's skill-format, review, security scan, versioning
      and improvement loop are ported rather than rebuilt (same owner).
      Agent drafts are never live by default: `activate_skill` returns a body as
      *instructions*, so an agent that absorbed injected tool output could otherwise persist it
      into every future session in the tenant. One live PR at a time:
      1. [x] (#433) `felix/skills/{format,binary,plugin,semver,review,security}.py` and the
         catalog loader reading frontmatter as YAML (line-reader fallback for anything YAML
         refuses), with Skillist's tests and `examples/skills/` as `fixtures/skills/`.
      2. [x] (#434, #436) Data model, migration, stores with conformance, `felix/skills/library.py`
         (draft / publish / rollback, publish policy), `create_skill` / `update_skill` tools,
         `spec.skill_authoring`. The publish policy was two settings
         (`FELIX_SKILL_PUBLISH_MIN_QUALITY`, `FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY`) until 4 added
         the per-tenant row.
      3. [x] (#437) `/skill-library` routes, `skills:write` scope, wire contract, e2e; with the
         review's carry-overs: an explicit pin to an operator upload wins over a library skill,
         and `update_skill` takes a required `parent_version` its approval binds, with the preview naming
         the parent and its inherited files by digest. Since fixed: the two bundle-write routes
         (`POST /skill-library`, `PUT /{name}/versions`) take up to 12 MiB, enough for a full 8 MiB
         bundle base64-encoded; every other route keeps the 1 MiB core cap.
      4. [x] (#442) Feedback (`submit_skill_feedback`, the `/-/feedback` inbox, accept / reject),
         improvement from accepted feedback into a reviewed draft, baseline-vs-with-skill evals,
         and the per-tenant publish policy (`require_eval`, `min_eval_uplift`), run by the
         worker's `skill_jobs`. Review fixes: tighten-only tenant policy with `DELETE`, rollback
         skips the eval requirement, only bundle-scenario evals count for an agent's version,
         pinned scenarios, leases with heartbeat / attempts / deadline, per-tenant job caps, fair
         claims, one sweep at a time, `expected_live_version` on publish, agents never build on
         a rejected draft. Follow-up: the sweep holds a `skill_job_lease` row instead of a
         session advisory lock, which leaked behind PgBouncer in transaction mode, and the fair
         claim cuts its tenant scan after ordering by last claim. Open: the sweep is a once-a-minute cron (the API does not enqueue to
         the worker); a skill that persuades the answering model to talk up its own answer is a
         residual risk to an eval-gated publish. Fixed since: the job caps, an agent's pending
         feedback cap and its pending draft cap count and insert in one transaction under a
         per-tenant (or per-manifest) `pg_advisory_xact_lock`, so they are exact.
      5. [x] (web#327) felix-web: client, vendored skill-format, library / editor / diff / review
         queue / inline chat card, evals, feedback and the policy form; publishes name the live
         version they expect. Open: the inline card was verified against a scripted model only.
      6. [x] (web#329, web#332) felix-web docs: concepts, manifest reference, management API,
         persistence, governance, deploy and observability.

#### B. Close the durable loop

- [x] **Run a fiber to suspension inside one claim.** Closed. The entry undercounted it: the
      cost is one tick *per op* plus one, not two overall — measured before and after rather
      than reasoned about, three stashes took four sweeps and now take one, a durable chat two
      and now one. A claim runs the fiber until it suspends, where suspension is exactly
      `status != "running"`. Fairness is what makes it safe and it is unchanged: a claim runs
      at most one `invoke` — the only op that can take seconds — so wall-clock per fiber per
      sweep is what it always was, and only the bookkeeping ticks go away.
      Two things the change turned up that the entry did not predict. The claim has to be held
      across the loop and released once at the end (`hold_claim`), because releasing per step
      and re-acquiring is *not* equivalent — `_renew_lease` only renews a lease this worker
      still holds, so the gap lets a second worker take the fiber and run the next `invoke`
      concurrently: a duplicated side effect, not a lost write. And `attempts` counts
      consecutive failures, which a landed step and a failed step sharing one claim would
      otherwise break — a failure after progress is charged as the first of a new streak.
      The entry's last sentence was stale rather than wrong: **there is no `heartbeat_at`
      column**, anywhere in the models or migrations. Sleeping is already distinguishable from
      crashed by `status` plus `lease_until`, which `_save_fiber` clears on every save.

- [x] **A gated wait hears more than its signal** (felix-run/felix#532). Four gaps on the
      durable path, one root each. `waiters.wait` moved the rest of a wait onto an in-process
      future after one failed BLPOP and never asked Redis again, while the API went on
      signalling through Redis -- an Approve that the run saw as a timeout; it now listens on
      both arms for the whole wait. An approval wait also reads its row every few seconds
      (`check`), so a decision whose signal is lost anyway is found from the record, and ends
      on the thread's abort flag (`denied`, note `aborted`, row closed); a client tool's wait
      ends on a Stop too (`[tool error/user_aborted]`). `client_bridge` kept a reported
      failure's text and dropped its `error` flag; it now returns a tool error. And a pending
      row whose wait died with its process is no longer listed as `pending`. The fifth gap --
      a reloaded tab left watching while its old hold lapses -- is the web client's: the hold
      is renewable only by its token, deliberately, so the fix is a quick retry after load.

- [x] **A re-run does not repeat what the run it replaces already did** (felix-run/felix#531).
      The assistant message holding a batch's tool calls was appended only once the whole batch
      returned, so a run that died mid-batch -- a worker restart, a lost fiber lease -- left no
      trace of calls that may already have taken effect, and the re-run asked the model again
      from the user's turn: on a production `cowork` thread the same files were written twice.
      The message is now written ahead of the batch (results after it), so the next run finds
      the call and `_interrupted_tool_results` closes it as "may have already taken effect" --
      and withdraws its gates with it: the pending approval (`close_interrupted`: `denied`, note
      `interrupted`) and the client request. The heartbeat no longer gives up after one failed
      renewal: it retries while the lease it last wrote still stands, stops the step one
      interval before it would lapse, and stops it at once when `_renew_lease` (which now says
      whether it still holds the claim) finds it taken. A lost claim is `FiberLeaseLost`, left
      to its new owner -- nothing charged, parked or released. What this does not do is record
      each call's result as it lands: a call that finished inside a batch that did not is still
      closed as interrupted, which errs towards telling the model to check rather than repeat.

- [x] **What a durable run is blocked on outlives its stream** (felix-run/felix#530). The durable
      `POST /chat/stream` closed at the run's `expires_at` -- 300s by default -- whether or not
      the run had stopped, and expiry is checked only between steps, so a durable chat (one
      step) went on. `cowork`'s approvals wait 600s. Only that stream announced a pending
      `tool_request` or `approval_required`, so everything the run asked for after the close
      reached nobody and timed out. `GateAnnouncer` now serves both loops: the reattach stream
      announces the thread's gates too (approvals behind `approvals:read`, as before), stays
      open past its idle limit while a durable run is in flight (on the short poll ceiling then,
      since a gate landing published no notification -- until gates announced, see "Durable
      streams stop polling at 10 s"), and the durable stream closes at its
      deadline only when no worker holds the run (`run_in_flight`, #529's predicate). Each
      stream announces a gate once; a client attached twice must dedupe by id.

- [x] **One durable run per thread** (felix-run/felix#529). A send to a thread whose durable run
      was still going started a second run beside it: the fiber did not record its thread, so
      nothing could ask, and the lease is advisory and the run holds none. The two appended to one
      log and neither saw the other's tool batch until it landed, so on a production `cowork`
      thread the same files were written twice, user turns landed between another run's tool
      calls and three write approvals were pending at once. `fibers.thread_id` (`0034`) records
      it; the enqueue checks and inserts under a per-thread advisory lock; both send routes
      refuse `409 run_in_progress:<resume_token>` after the idempotency key is judged, so a
      resend still reattaches; and the snapshot names the run as `activeRun`, the handle a
      reloaded client had no way to get. "In flight" is not "not terminal": a run past its
      expiry that no worker holds does not count, or a deployment with no worker would lock the
      thread for good. The two gaps left open on the same path, #530 (client tools after the
      stream's deadline) and #531 (a re-run repeats tool calls), closed in the same release.

- [x] **A durable run streams its transcript** (felix-run/felix#238). `POST /chat/stream` on a
      durable manifest sent `run_accepted` → `run_status` → `final` and nothing between, so the
      answer arrived and the tool calls behind it did not. Correction to the premise this started
      from, and it matters for the entry below: bridging `side_events` through Valkey fixes
      nothing here, twice over. `drain` is called inside the agent's own loop and `emit` comes
      from tool execution in the same process, so on a fiber **both ends are already in the
      worker**; and the events do not exist to be bridged anyway, because `invoke` is
      `_run(..., emit_events=False)` — the deltas and `on_tool_start`/`on_tool_end` are dropped
      at the source and the `drain_side_events` loop is itself inside `if emit_events:`. What the
      fiber does produce is the session log, written incrementally by `_append_produced` outside
      every `emit_events` guard. So the stream now tails it, through the same helper
      `GET /chat/stream/{thread_id}` uses. No new transport, no second source of truth. Completed
      messages only — chunks are never persisted, so a durable run never streams token deltas.

- [x] **Split the SSE tail out of `routes/chat.py`** — `routes/_streaming.py`, beside
      `routes/_sse.py`. Deferred from #238 as motion that would have obscured the change it rode
      in on, and done first here because the approvals entry below is itself a streaming change.
      `chat.py` was 1,774 lines and the largest module in the repo; 417 of them were one subject —
      *tailing a session log over SSE, and how fast to ask* — with no route decorator among them
      and no importer outside that file and its tests. The division against `_sse.py` is that it
      knows the frame *envelope* and this knows the *source*. Names lost their leading underscore
      on the way, matching `_sse.py`: a private module with a public surface. Verified as a pure
      move by diffing the relocated blocks against `main` modulo the renames — the only other
      changes are one comment re-attached to the constants it explains (it had drifted onto
      `RUN_TERMINAL`), `json` hoisted to module scope, and a return annotation.

- [x] **An approval says why it fired and what it blocks** (migration
      `0015_approval_reason_and_call`) — the half of the entry below that does not need a
      transport. The two channels were each missing what the other had, and both sides of the
      wire had already written it down: `builder.py` at the emit ("the `/approvals` row does not
      carry it either") and `@felix/client`'s `PendingApproval`, which documents `reason` as
      frame-only and `expiresAt` as poll-only. So the row gains `reason` and `tool_call_id`, the
      frame gains `expires_at` read off the row, and `list_approvals` takes a `thread_id` filter
      (under-reporting by construction, since `create_pending` reuses a row across threads). This
      matters most on the durable path, where the poll is the only channel and was the half that
      could not say *why*.

- [x] **`approvals` can be reclaimed, and `jobs/retention.py` stopped claiming it already was.**
      The docstring listed the table as "bounded by ... its run or job"; there is no FK, no
      cascade and no `delete(Approval)` anywhere, so it grew for the life of the deployment, and
      nothing moves a timed-out row off `pending` either. Closed by *both* halves of the choice
      this entry offered, because they were not alternatives: `FELIX_APPROVAL_RETENTION_DAYS`
      (default 0 = keep) sweeps **settled** rows on both backends, and the docstring now says
      what is and is not bounded. Settled is the exact negation of `find_approved`'s predicate —
      an unexpired `approved` grant is never swept however old, and neither is one with a null
      `expires_at`, because retention must not silently revoke authorization from a cron job.
      Off by default, matching session retention: the row is the record of a human decision and
      the `soc2` / `eu_ai_act` profiles lean on it.

- [x] **A2A `taskId` went off the wire straight into a thread id.** Closed, and it was not
      only A2A: `eval/runner.py` composes `{tenant}:eval:{run}:{item_id}` the same way, and
      dataset items arrive through `PUT /eval/datasets/{name}`, so that id is caller-supplied
      too. Both skipped every rule `effective_thread_id` applies — the `#` rejection and
      `MAX_THREAD_ID`. The cap turned out to be load-bearing rather than hygienic: `thread_id`
      is the tail of the `session_events` primary key and `task_id` half of the `a2a_tasks`
      one, both btree, so an incompressible id past ~2700 bytes *fails the insert* ("index row
      size 3864 exceeds btree version 4 maximum 2704") rather than merely bloating the index —
      a 500 repeatable at the rate limit. Verified against a throwaway Postgres, because
      `memory://` keys a dict and shows none of it. Both sites now compose through
      `thread_ids.a2a_thread_id` / `eval_thread_id` — one composer per namespace, so which
      segment may carry a `:` is fixed by a signature rather than by call-site arity. A2A
      answers `-32602` before writing the task row, and an
      eval item with an unusable id fails that item rather than the run. `:` stays legal in the
      last segment so `urn:uuid:…` task ids keep working. `felix_api/threads.py` moved to
      `felix/thread_ids.py` to make one definition reachable from the harness. The security
      review then found the half this missed: `PUT /eval/datasets/{name}` writes `item_id`
      into the `eval_dataset_items` primary key, so the same insert failure sat one route
      *earlier* than the new guard. `validate_items` refuses it now — a new 422 on ids that
      were already unrunnable.

- [x] **`replica_id` was `"local"` on every worker, so lease ownership named nothing.**
      `durability/fibers.py` decides whether a claim is its own with
      `lease_owner == replica_id`, and nothing ever set `FELIX_REPLICA_ID` — not the chart,
      not a Compose overlay, and `validate_runtime()` did not require it under `scale_out`.
      Every worker therefore claimed under one name and those predicates matched each other's
      claims. Found by the security review on #262. Closed both halves, because the default
      and the deployment were separate failures: the default is now `{hostname}:{pid}` —
      host and pid rather than a uuid, since in Kubernetes the hostname is the pod name and
      this is a value an operator reads — and the chart sets it from the downward API in the
      env tier every deployment shares. Compose needed no change: containers already get
      distinct hostnames, checked rather than assumed. An empty value is refused outright: it
      would be *worse* than the constant, because `lease_owner` is `""` on every unclaimed
      row. Nothing was broken end to end — claim exclusion rests on `lease_until` plus
      `FOR UPDATE SKIP LOCKED` — but the second line of defence those predicates are written
      to be was absent.

- [x] **The `ui` waiter is a bearer capability with no tenant in it.** `ui:{request_id}` carries
      no tenant (`ui/prompts.py`), and `POST /chat/ui` does `_ = request` — no tenant, no thread,
      no ownership check (`routes/chat.py:952-963`). The whole control is the secrecy of a 96-bit
      `token_urlsafe`, which is adequate in practice (it is emitted only on that thread's side-event
      stream, and stream access is tenant-gated) but is the one surface where every other route
      checks ownership and this does not. The fix is `waiter_name("ui", thread_id, request_id)`
      plus a `thread_belongs_to_tenant` check — deliberately *not* folded into #250, because it
      changes the `ui` name shape that PR's upgrade note promises is unchanged, so it wants its
      own commit and its own note. Decided and done: `ui:{thread}:{request_id}`, `thread_id`
      required on `POST /chat/ui`, with the breaking-change note in the CHANGELOG.

- [x] **`waiters._local` never shrinks on the signal-first path.** A `signal` with no waiter
      registered a *completed* future and only `wait` popped it, so while Redis was in fallback
      an authenticated caller POSTing `/chat/tool_result` with random `tool_call_id`s grew the
      dict without bound. Closed by #290, which left this entry open: signal-first entries are
      capped at `waiters.MAX_LOCAL_SIGNAL_FIRST` (1000), oldest evicted — the same at-most-once
      contract the fallback already had. `tests/unit/test_waiters.py` goes red without the
      eviction.

- [x] **`tool_call_id` was provider input spliced into a `:`-delimited waiter key.** Closed, and
      the entry understated it: the collision needs no hostile `tool_call_id` at all, because
      *`thread_id` already contains colons* -- `{tenant}:{suffix}`, and `{tenant}:fiber:{id}` for a
      durable run. `fiber` is a legal thread suffix, so a caller can create `acme:fiber` and post a
      `tool_result` for call `F123:call_9`, forging the waiter of the durable run on
      `acme:fiber:F123` answering `call_9`, and satisfying its pending client tool with content
      they chose. Same tenant only; the tenant prefix cannot be forged. Waiter names now go through
      `waiters.waiter_name`, which percent-encodes each part (`%` before `:`) so the join is
      injective; approval and UI names are byte-identical since their ids are a uuid and a
      `token_urlsafe`. The repo's named defect shape, and worth noting that the *second* grammar
      here was one the harness minted itself rather than one it received.

- [x] **Approvals reach the durable path.** Closed in two halves, and *not* the way this entry
      proposed. It said to route `side_events` through the Redis layer from `#93`; that would have
      been wrong for the reason the Valkey bridge was wrong on #238, and the reason is in
      `notify.py`'s own docstring: "the notification is a hint, never the source of truth", because
      every wake re-reads Postgres. Pub/sub is at-most-once, and here the message *is* the frame —
      a dropped one means the run blocks its whole `ttl_seconds` and then denies with no human ever
      asked. So instead: felix#245 put `reason` and `tool_call_id` on the approvals row (the frame
      carried them, the row did not), and the durable stream now reads the pending rows for its
      thread and **rebuilds** `approval_required` from them. Same principle as the transcript tail,
      applied to the half the session log cannot carry — `_append_produced` writes assistant turns
      and tool results, and a request for permission is neither. Announced once per stream, deduped
      by id because `list_approvals` answers "what is pending" and has no cursor; thread-scoped,
      because `GET /approvals` is tenant-wide and every pending approval in the tenant on one run's
      stream would leak other conversations' tool names and arguments. The poll remains the channel
      of record: a stream that was never open sees nothing.

- [x] **Signed completion webhooks**, delivered from the **worker** — the fiber reaches terminal
      state under its cron and the API replica that accepted the request may be gone. Dead letter
      is `status='dead'` on the same durable row, not a second store. `spec.webhooks` selects
      operator-registered endpoint ids and **never carries URLs**: a manifest author holds a
      tenant scope, and a tenant-supplied URL on a path carrying run output is an exfiltration
      channel SSRF checks do not address. Shipped as `spec.execution.webhooks` (durable
      only) over `FELIX_WEBHOOK_ENDPOINTS`; delivery state is three columns on `fibers`
      (migration `0019`), claimed `FOR UPDATE SKIP LOCKED` by a `webhook_delivery` cron, so the
      Temporal path is covered by the same sweep. Egress guard unless `private: true`.

- [x] **Bound the retry.** Correction to the entry as written: an `invoke` that raises is
      terminal in one tick (`status: failed`); it was the failures *outside* that handler — a
      save, a lease write, a store down — that were released and re-claimed once a minute until
      `expires_at`. Now: `fibers.attempts` (migration `0013`), backoff 1m→1h doubling, and
      `status: dead` at `FELIX_FIBER_MAX_ATTEMPTS` (5), with the error on the run view and every
      terminal-status set (`sdk.py`, the resume stream) agreeing under an invariant.

- [x] **Non-streaming `/chat` approval visibility.** `invoke()` never drains `side_events`, so a
      caller blocked on an approval hangs for the full TTL and then receives a deny, never
      learning an approval was requested. Closed by recording every side event on the request
      context as well as its stream queue: `POST /chat` now answers with `approvals`, each the
      frame the stream would have sent plus `approved` / `denied` / `expired`. A blocked caller
      still has no frame — it finds the id with `GET /approvals?thread_id=` — because a plain
      HTTP response cannot say anything until it is over.

- [x] **A crash mid-tool-loop resumes instead of replaying the whole `invoke`.** Closed
      without the `ctx.step` / `fiber_steps` table this entry prescribed. The session log
      already journals every model turn and tool result as it happens, and a replay was bad
      because it *ignored* that: it re-sent the user turn onto a thread that held the run's
      progress, so the model answered the request twice and could repeat calls that had
      taken effect. Now the step records the log head (`invoke_began`, written under the
      claim's version so the lease loop's lost-write check is unchanged) and a re-claim
      continues from the log, or takes a reply already logged without calling the model.
      `_interrupted_tool_results` still closes the one call that was in flight, which is the
      only part no journal can settle. The run's own user turn is what marks its part of the
      log — events the loop writes ahead of it are not the turn. Still re-sent: `checkpointer:
      none`, composite patterns (they route or score from the incoming turn), `semantic:N`,
      input-redacted requests. Open edge: on a caller-owned thread, a reply to a request sent
      between crash and re-claim reads as the run's; closing it means recording the logged
      turn's `event_id` in the marker after the append.

#### C. Operator console

- [x] **The summarizer is metered.** Found by the 2026-09-04 readiness audit, not on this
      list: compaction billed its summarizer call to the literal tenant `"default"`, priced it
      by the logical route name, and never touched `limit_state`, so the largest input-token
      call in a long thread escaped `max_cost_usd`; `summarizing:N` recorded nothing. Both go
      through `record_model_usage` now, and the react loop's reported usage block is priced by
      the wire id (it was the logical name, so every custom route reported `$0` on the turn).

- [x] **Persist cost.** Migration `0011_usage_cost`: `cost_usd` and `wire_model_id` on every
      row, priced at write time by the wire id and any `spec.model.price` override (which was
      documented as doing this and decorated only the `/v1/models` listing). Stored `model_id`
      stays the logical route name, which is what an operator recognises.

- [x] **`GET /usage/summary`** — by manifest / model / UTC day, with totals; both backends
      under conformance.

- [x] **Usage by thread.** Migration `0035_usage_thread`: `thread_id` on every usage row, the
      `{tenant}:{suffix}` the audit payload carries (`''` outside a thread, and on every row
      written before it). `GET /usage?thread_id=<suffix>` filters; `GET /usage/threads` groups a
      window by thread, newest activity first, with totals over every thread and `truncated`
      when the page holds fewer than exist. Both backends under conformance.

- [x] **Fill the missing bundled rates** — ~~`gpt-4.1` has no entry and bills at the default~~ (priced in #301, with `-mini` and `-nano`).
      Correction to this entry as written: an unpriced model contributes `$0`, so
      `limits.max_cost_usd` fails **open** for it, not closed — `felix_model_unpriced` now says
      when that is happening. The "long-context tier" it left open was not missing: see below.
      What *was* wrong, checked against Anthropic's pricing page on 2026-09-30: `claude-sonnet-5`
      billed at Sonnet 4's $3/$15 (it is $2/$10), and two point releases matched their
      predecessor's key by substring — `claude-opus-5-5` paid Opus 5's $5/$25 (it is $4/$20,
      cache reads $0.20) and `claude-fable-5-1`/`claude-mythos-5-1` read cache at $1 (it is
      $0.25). All over-charges, so `max_cost_usd` stopped runs early and usage over-reported.
      Every bundled Claude rate is now pinned field by field in `test_model_capabilities.py`.

- [x] **An approval row names the thread it is blocking** (migration `0014_approval_thread_id`,
      felix-run/felix#232). The `approval_required` frame carried `thread_id`; the row did not —
      and the two channels do not cover the same runs. Side events are an in-process queue keyed
      by thread, so a durable run (agent in the worker, stream served by the API) is reachable
      only through `GET /approvals`: the channel that is the whole story for an unwatched run was
      the half with nothing to attribute. It is the *originating* thread, because `create_pending`
      still reuses a pending row across threads. Widening that reuse key would change grant scope
      and is a product decision, not part of this.

- [x] **Attribute denials in the audit record.** Landed: `policy_deny` rows carry
      `payload.control` naming the wrapper that refused — the source was on every deny output
      already (`deny_output` stamps it) and the loop was the one reader that dropped it, so the
      fix was a read, not a design. And the auditor's second question: `GET /audit/export`
      streams a time range as JSONL, uncapped, with `since`/`until` as a half-open range applied
      in the store (both arms, conformance-pinned) so windows tile. Open-ended, it reads only
      what has been flushed; an export whose `until` is in the past is stable.
      Pinned under an enforcing policy in `test_rls_enforcement.py`: the production stack
      exports every page, and the route's per-read `rls_tenant` holds on its own with no
      middleware. Correction to this entry as written: a lost scope does not end the file
      early unless *every* binding goes — the request-wide one `async_run_with_context` makes
      covers the streamed body, and the session listener falls back to the request context —
      so the per-read binding is a second guard, not the only one.

- [x] **Surface eval instrumentation.** Correction to the entry as written: there was no
      `ItemScore` and no per-item duration or token count stored anywhere — only `tool_calls` /
      `tool_errors` on the score row. Each item now records `duration_ms`, `tokens_input`,
      `tokens_output` and `cost_usd` for the candidate's own turn (read off the item's
      `LimitState`, which metering already fills; the judge's cost is the eval's, not the
      agent's), and every run dict carries `stats` summed from its rows plus `wall_ms` — derived
      on read, so no migration. The judge's fail-open path is visible: `judge_fallback` /
      `judge_error` on the row, `stats.judge_fallbacks` on the run, a warning from
      `felix eval`, and `--strict-judge` to fail on it.

- [x] **Skills routes.** Landed: `GET /skills/{manifest}` lists what a manifest can reach and
      what is active, `GET /skills/{manifest}/{skill}` returns the body `activate_skill` would
      hand the model, and `GET /skills/{manifest}/activations/recent` says which skill activated
      on which turn — all on a new `skills:read` scope, kept off `manifests:read` because a skill
      body is prompt content. Read-only: activation is the model's decision mid-turn and the
      store is keyed by `(tenant, manifest)` rather than thread, so an operator writing it would
      be racing a run with no turn to attribute the change to.
      The third ask needed a fix to be answerable at all: `tool_runner` audits every tool call
      but its payload carries the tool's *name*, not its arguments, so the trail said a skill
      activated and never which one. `skills/tools.py` now emits `skill_activation` naming it —
      safe to store because `activate` resolves the name against the catalog first.

- [x] **Decided: `spec.skills` only adds, and a manifest can now opt out of that.**
      `spec.skills_declared_only: true` makes the declared names the whole catalogue. Opt-in
      rather than a default change, because narrowing silently would alter behaviour for every
      manifest already in Postgres — a migration, by this repo's own rule, not a
      reinterpretation. The reason to want it: a skill body is appended to the system prompt,
      so an ambient skill is the one prompt-shaping input `pin_compile` cannot cover, since the
      hash is over the manifest and the drift is on the host's disk. `GET /skills/{manifest}`
      reports `declared_only` so the two readings are distinguishable from outside.

#### D. Truth in advertising

Small, and blocking for the adopter goal: anyone evaluating Felix on its governance claims reads
`governed.yaml` first. Enforce or delete, per item.

- [x] **`governed.yaml retention_days: 30` is inert.** Wired: the nightly sweep prunes the
      manifest's own `audit_events` past that many days, capped by `FELIX_AUDIT_RETENTION_DAYS`
      (a manifest shortens the deployment TTL, never extends it). Removed from
      `test_inert_manifest_fields.py`.

- [x] **`governed.yaml:128 guardrails.targets: [input, output]` does not scrub replies.**
      Implemented: the reply-path wrapper redacts (or blocks) PII in the agent's reply on
      `invoke` and on the streaming path; `output` covers tool output and the reply,
      `final_response` the reply alone. `deploy/GOVERNANCE.md` says so.

- [x] **Five `PlanExecuteSpec` fields were inert** — four are wired and one is gone.
      `planner_model` and `executor_model` route the planning call and the subtask agent;
      `replan_on_failure` and `max_replans` replan the *remainder* when a step ends early, on a
      deliberately narrow definition of "early" (`_FAILED_STOP_REASONS` — a refusal or a cut-off
      answer, not an empty one). `planner_few_shots` was removed rather than wired: it named a
      count of examples with no corpus behind it, so there was nothing to make it mean, and it is
      in `RETIRED` so stored manifests keep loading. Two things found on the way.
      `_pipe_stream` keeps the *last* terminal event and `react` puts `stop_reason` on `done`
      as well as `on_chain_end`, so a test double that omits it reports every streamed step
      as `end_turn` — which looks exactly like a streaming bug in the pattern; the composite
      `_terminal_events` had the same hole, which also meant `/v1` reported a default
      `finish_reason` for every streamed composite run. And `executor_model` was wired first
      on `_DelegatingAgent`'s `self.inner or self._base_agent(...)` fallback, which is dead
      from core because `_build_plan_execute` always passes `inner` — the field stayed inert
      *and* the textual ratchet started reporting it as fixed.

- [x] **The session log keeps the unscreened reply.** Fixed at the write rather than by
      either prescription. A redaction event would have left the raw PII stored and every
      reader owing the replay logic; handing the wrapper the store would have come after the
      live tail had already published the raw turn. Instead the compile hands the pattern a
      `ScreenedSessionStore` whenever reply controls are on. It redacts every assistant
      message and judges each reply before it is appended, through one `ReplyScreen` shared
      with the reply wrapper, so the log and the client get the same verdict and a judge runs
      once. Children compile against the same store, because the router forwards the caller's
      thread; reflect quotes its draft through the screen. Remaining: a preamble before tool
      calls is redacted but not judged, so on a denial it stays in the log; stored reasoning
      and compaction summaries are kept as written (`deploy/GOVERNANCE.md`).

- [x] **Memory capture reads the unscreened reply.** `ReactAgent._maybe_capture_memory` handed
      `capture_from_turn` the pattern's own `final`, from inside the wrapper, so a manifest
      with PII guardrails and `memory.capture` could store a fact extracted from the text the
      reply controls redacted. Now it extracts from `ReplyScreen.settle` — redacted, and
      skipped when a judge denied the reply. Screens chain (`ReplyScreen.parent`) down the
      sub-agent tree, so a router's controls reach a child's capture even when the child has
      controls of its own.

- [x] **Final-response judges do nothing on the streaming path.** Fixed with the reply-path
      wrapper: reply text is held until the run ends and released judged, or replaced by the
      denial; structural frames still stream as they happen.

- [x] **Inbound screening skips two paths.** Correction to the entry as written: the durable
      fiber path *was* screened, at `/chat` before enqueue; the unscreened paths were cron
      jobs (a prompt writable with `jobs:write`), eval items, `/chat/continue` and MCP
      `tools/call` arguments. The screen is now a wrapper the compile puts around the agent
      (`InboundScreeningAgent`), so every path that runs the agent screens without a list of
      paths; MCP screens the argument tree instead.

#### Harness

- [x] **Moved off `psycopg[binary]` to `psycopg[c]`** — done well before the ignore expired,
      and `.trivyignore.yaml` is empty again because the findings went away with the library
      rather than being suppressed. One correction to this entry as written: the swap is in
      `deploy/docker/Dockerfile`, **not** in `pyproject.toml`. Changing the dependency would
      have made `pip install felix-harness` and every contributor's `make install` require a
      compiler and libpq headers, which is a steep price for an OSS project to pay for a
      property only the shipped image needs. The image installs `psycopg[c]` over the synced
      venv and asserts `psycopg-binary` is gone; everywhere else keeps the wheel.
      The measurement the entry asked for: **577 MB against 587 MB**, so dropping the vendored
      libraries more than paid for `libpq5`. `psycopg.pq.__impl__` reports `c`, migrations run
      to head against real Postgres 17.11, and the scan exits 0 with no ignore file at all.

- [x] **`session.context_window_tokens` should default to a sentinel, not a number.** Done: the
      default is `None` (the model's window), `runtime.py` no longer reads `model_fields_set`,
      and the pin hash now tells a written 128000 from an omitted field. Found on the way: the
      model listing (`usage/catalog.py`) read this field, so every manifest listed a 128K window,
      and its fallback looked the window up by the manifest's *name*; it uses the function
      compaction does now. The bundled manifests keep their explicit 128000, so they compact as
      before. As written: it is the
      one field in the schema where writing the default and omitting it mean different things:
      `runtime.py` reads `model_fields_set` to tell them apart, so an explicit `128000` compacts
      against 128K while omitting it compacts against the model's real window (1M on a
      large-context route). Everything else in the repo treats the serialized form as the
      meaning — including the compile-pin hash, which cannot see the difference and never could.
      Making the default `None` would make the two agree, remove the only `model_fields_set`
      read outside `Settings`, and let the pin notice a change that currently slips past it.
      Touches `react.py`, `usage/catalog.py`, four bundled manifests and
      `test_compaction_window.py`, so it wants its own change rather than riding along.

- [x] **Temporal: decide.** Decided 2026-10-02: **removed.** Fibers are the one durable path;
      `FELIX_DURABILITY=temporal` fails startup naming what to do, and the fiber scheduler now
      claims rows an earlier version handed to Temporal. The original note follows.
      (`make up-temporal` now runs it end to end, and the backend's
      writes actually persist — see CHANGELOG — so the decision can be made against something
      that works. Still no TLS/API-key on `Client.connect`, so Temporal Cloud is unreachable.)
      Original note: The arm is a ~165-line driver loop (`temporal.py` + `_temporal_workflow.py`)
      using none of Temporal's durability primitives — no signals, no queries, no child workflows,
      no `continue_as_new`, no activity retry policy. State still lives in the Postgres `Fiber`
      row, so an operator choosing it for Temporal's guarantees gets Felix's. Four of its six tests
      assert only that the classes can be constructed, and there is no integration test against a
      dev server. It does fix the one-op-per-tick problem — which running a fiber to suspension
      inside one claim (§B, #262) now fixes for every backend. Either invest properly or document
      it as a compatibility shim.
      (2026-09-29: no Temporal code change since #183; the fibers path meanwhile gained #262,
      #336, #339 and a per-worker poll loop in #363 that starts a run within a second.)

- [x] **Live-model eval (optional CI)** — the gate is now a pair of mock fixtures: `smoke.json`
      passes by construction and `negative.json` must fail, checked by
      `scripts/eval-counter-smoke.sh` in both CI and `make check-ci`. That proves the scorer can
      say no, which it could not before, but both halves still score a canned answer — nothing
      here scores the agent. Optional nightly against `api.felix.run` that does not block PRs.
      Done 2026-10-02, at HEAD rather than against `api.felix.run` (decided: it catches a
      regression before deploy, and `contributor` needs the checkout as its workspace):
      `.github/workflows/eval-live.yml` runs `quick` on the new `fixtures/eval/live.json` nightly
      and `contributor.json` weekly with `--strict-judge`, via `scripts/eval-live.sh`, which
      writes a per-item table with cost to the job summary. Needs repo secret
      `FELIX_ANTHROPIC_API_KEY`. Its failures now feed `triage` as rank-1 evidence beside
      `smoke.yml` (`manifests/triage.yaml`, `docs/SELF.md`, the `felix-self` skill), counted only
      when the same item failed in that job's previous run too. Not counted in the scoreboard's
      rung-3 "regressions" metric: charging a nondeterministic eval failure to a Felix merge is
      a separate decision.

- [x] **Validate eval dataset items.** Done: `felix/eval/validation.py`, called by
      `PUT /eval/datasets/{name}` and by `felix eval --fixture`. An item with no `user_input`
      is refused and the near-miss key it used is named back (`input` — the spelling the
      bundled fixtures once used — plus `prompt`, `question`, `query`, `user_message`, `text`);
      so is a non-object rubric and a repeated `item_id`. A rubric naming no rule is legal and
      warns instead, because it scores as `nonempty` and passes anything. The rubric stays
      free-form. `tests/e2e/test_mgmt_routes.py` pinned the old accept-and-store-empty
      behaviour and said it should fail when this landed; it now pins the refusal.

      One consequence for the item below: an unscoreable rubric no longer reaches the runner
      through `--fixture`, so the counter-smoke's fourth check is unreachable from a fixture
      and now guards only the paths that bypass validation — items written straight to the
      store by the continuous-eval job, and a manifest that fails to resolve.

- [x] **An eval run cannot report how many items errored.** `error_count` is on the run row
      (migration `0017`) as the subset of `fail_count` that never reached the scorer;
      `fail_count` keeps meaning "did not pass", which the CLI exit code relies on. The
      counter-smoke checks both, so the row and the score rows cannot drift.

- [x] **Memory defaults** — `FELIX_MEMORY_EMBEDDER` defaults to `auto`: the local bge model when
      `felix-harness[embeddings]` is installed, no vector channel otherwise. Local only by design —
      a hosted embedder would send every stored memory to a provider, and the column is 768 wide
      where OpenAI's is 1536. Memory-on-by-default is a new `assistant` manifest rather than a
      change to `quick`: memory is keyed by tenant and manifest, not by caller, so on a shared or
      anonymous deployment one caller's facts would reach another's prompt. `assistant` refuses
      anonymous callers; `quick` stays stateless. `tests/e2e/test_assistant_manifest.py` carries a
      fact from one session into the next. The meta filter now catches 13 of 13 on
      `tests/unit/test_memory_meta_filter.py`'s corpus (was 3), refusing none of the 8 user facts.

- [x] **Memory consolidation** — decided: merge duplicates only. `spec.memory.consolidate`
      now drives a model pass in the `consolidate_memory` cron after the exact-hash dedupe:
      per (tenant, manifest) pool, resolved like a request, the model names groups of
      agent-written facts that say the same thing (ids only), and `memory/store.py:merge_duplicates`
      keeps the oldest member of each group (never a model-chosen one) and supersedes the rest,
      refusing operator rows, mixed kinds and differing `topic_key`s (absent included) on both
      arms. No summarising or rewriting — a fact is never authored by consolidation. Spend: a
      pool whose batch fingerprint matches its last clean pass is skipped, and 50 pools per tick
      at most reach the model. Still open: the fingerprint lives in the worker process, so a
      restart costs one call per enabled pool, and two workers each keep their own; overlapping
      ticks are data-safe (the store re-plans under a row lock) but can each pay for one call.
      Persisting the fingerprint would need a column or table. As written: —
      `consolidation.py` is 14 lines against `extraction.py`'s 340, so the store only grows.
      With `assistant` capturing on every long turn, that is now a default-path growth rather
      than an opt-in one.

- [x] **Who may retire a memory by naming its `topic_key`** — decided: the operator only.
      `memory/store.py:_may_retire_by_topic` requires rank above `_DEFAULT_TRUST` for the
      topic sweep on both arms; agent writes are stored alongside, and the facts prelude shows
      one current value per topic (trust, then turn). The `remember` tool no longer tells the
      model a new value supersedes the old. As written: — `put_memory` supersedes any active
      row sharing a `topic_key`, and `capture_from_turn` reaches the same supersession post-turn
      through no governance wrapper at all. The durable fix is store-level: require rank above
      `_DEFAULT_TRUST` for a cross-row sweep, so rank-1 writers store alongside rather than
      retire. A real ergonomic change, which is why it is a decision and not a patch.

- [x] **`deploy/GOVERNANCE.md`: which layer owns retirement** — the store; the new "Memory: who
      may retire what" section says so. As written: — follows whichever way the above
      lands. `retired_by` versus `source`, why resurrection is gated on who retired rather than
      who wrote, and which of the manifest, the store and the approval wrapper is authoritative.
      Enforced in `tests/conformance/test_memory_trust_matrix.py`; the prose does not exist.

- [x] **Warn when `when_args` names nothing** — decided: both, by what is knowable.
      `manifests/approval_args.py` refuses at write (`validate_for_write`, so `PUT /manifests`
      and `felix validate-manifest`) a rule naming built-in, plugin or memory tools literally
      when a `when_args` name is none of their arguments; it warns at compile, once per process,
      with `felix_approval_when_args_unknown`, for every resolved tool — MCP included, since
      those schemas can change under a stored manifest and refusing would be an outage. A name
      is flagged only when no reached tool takes it, so `github__*` with `when_args: [force]`
      is fine; a tool whose schema lists no properties is never grounds. Not a parse-time check,
      which would reject stored manifests on read.

- [x] **Split-turn compaction** — when the kept window starts mid-turn, the history before the
      turn is summarised as before, the turn's opening user message is kept verbatim (capped at
      `OPENING_MESSAGE_MAX_CHARS`, 16,000), and the steps between it and the kept window get their
      own prompt and a 2,048-token budget (`TURN_PREFIX_PROMPT`, rendered by `turn_prefix_message`:
      user tier, labelled, fenced). Both lead the checkpoint's `retainedTail`
      (`metadata.split_turn.lead_items`), a second cut through the same turn folds the earlier
      progress in, and a cut past the turn moves both into the history summary. Not narrow, as
      this entry used to claim: `contributor` and `triage` keep 20,000 tokens and a turn of theirs
      is ~38k, so every prior turn was split and its ticket survived only as a paraphrase.
      Remaining: no live-model run has judged the turn-prefix prompt's output yet (the tests use a
      scripted summariser), the history summary still has no output budget of its own, and a
      pinned event older than a checkpoint's cut is in neither its `retainedTail` nor the re-walk,
      so a replay drops it (pre-existing; it now matters for a pinned request).

- [x] **Tools carry their own prompt copy** — `Tool.prompt_guidance` for tools defined in code,
      and `spec.tool_guidance` (tool name or glob → one line) for everything a manifest binds,
      MCP included; `builder.tool_guidance_section` appends a "Tool guidance" section built from
      the tools the agent actually has, so advice for an unbound tool never reaches the prompt.
      `system_prompt.include_tool_guidance: false` turns it off; an entry matching no tool counts
      as `felix_rule_targets_nothing`. `deep.yaml`'s search/fetch advice moved there. Built-in
      tools carry no guidance yet — adding model-facing text to every manifest wants an eval run
      first. Followed by `McpServerRef.use_instructions`: an opted-in server's `initialize`
      `instructions` become its tools' guidance, capped, one line, dropped when the injection
      markers flag them — opt-in because it is server text in the system prompt.

- [x] **Telemetry vocabulary** — `docs/OBSERVABILITY.md` carries the metric catalog and the span
      schema, and `tests/unit/test_metric_catalog.py` re-derives it from the source so it cannot
      drift. Spans now follow the OTel GenAI semantic conventions, and a model call is a span at
      all (it was not). The `metrics.py` silent degrade on a reused label set is unchanged and is
      documented as a known limitation rather than fixed.

- [x] **Ship one dashboard** — `deploy/docker/compose.observability.yml` (`make up-observability`)
      brings up an OTel Collector, Prometheus, Grafana, Jaeger, Loki and Postgres/Valkey exporters,
      with a provisioned `Felix — harness overview` dashboard whose governance row surfaces the
      counters GOVERNANCE.md tells operators to watch. A gated `serviceMonitor` template covers the
      Kubernetes side. The scrape credential is a zero-scope API key, because `/metrics` is
      authenticated on purpose.

- [x] **Decide on a JWT verification cache** — decided 2026-10-02: **no cache**; written into
      `deploy/GOVERNANCE.md#jwt-verification`. Revisit only with a measured cost. The original
      note follows. `verify_jwt` verifies signatures on the event loop
      for every request in `jwt` mode. A TTL cache keyed on the token digest removes the repeat
      cost, but a cached "valid" survives a revocation for as long as it lives. That is a posture
      call about how stale an authorisation may be, and it wants an owner rather than a default.

- [x] **Keep growing the fiber scheduler, or make Temporal the documented multi-step path.**
      Decided 2026-10-02 with the item above: keep growing the fiber scheduler; Temporal is gone.
      Temporal already wraps the same `advance_fiber`; what fibers duplicates is the scheduling
      envelope, and that is where this audit's durability bugs were — a lease that equalled the
      approval timeout (#150), resolution outside the tenant context (#150). The §B item this
      pointed at (step memoization, an append-only `fiber_steps` table — an activity model by
      another name) closed in #339 *without* the table, so nothing is blocked on this any more;
      since 2026-09-02 fibers gained run-to-suspension (#262), completion webhooks (#336),
      crash-resume from the session log (#339) and a 1s per-worker poll (#363), and Temporal
      nothing. **Still a decision**, now weighted by that.

- [x] **`cowork.yaml` sets `auth.inbound.allow_anonymous: true` on a manifest that binds a
      local shell.** Now `false`, with the reason in the manifest. Checked at the same time:
      `validate_runtime` already confines `auth_mode=none` to loopback, so the reachable case
      was the developer's own machine. Also landed: `PUT /manifests` refuses plaintext
      credentials and disallowed sandbox images at write time, and `GET /manifests/{name}`
      redacts an embedded credential — `manifests:read` could read one before. The `client-shell` approval rule and the `thread_id`/`tool_call_id`
      requirement are what stand between an anonymous caller and command execution on a
      developer's machine. Untouched by the audit; wants a conscious yes or no.

- [x] **A per-tool screener cost lever.** `content_screening.model_tools`, as prescribed: a glob
      list of which screened tools get the paid scoring (`model` and `decider`); empty is every
      screened tool; the marker scan stays unconditional, so leaving an untrusted tool out
      screens it by markers alone rather than not at all. Refused without `model` or
      `decider: true`, and unmatched patterns count as `felix_rule_targets_nothing`.

- [x] **Fiber rows are never swept** — and neither were `usage_events`, `a2a_tasks` or
      `session_events`; four tables grew for the life of a deployment. The sweep now covers
      every appended table on both backends, with `FELIX_{AUDIT,USAGE,FIBER,SESSION}_RETENTION_DAYS`
      in place of module constants (`0` keeps; sessions keep by default). The memory:// arm
      never pruned audit rows at all — it filtered the `DurableBuffer` as if it were the list —
      and swept plans for tenant `default` only; both fixed, and `tests/conformance/test_retention.py`
      runs the contract against both arms.

- [x] **Temporal carries `state["auth"]` into workflow history.** Documented as an assumption in
      `deploy/GOVERNANCE.md` (Temporal is inside the trust boundary) rather than changed. `start_fiber_workflow` passes
      the whole fiber dict as the workflow argument, and the activity re-passes it per step, so
      `{principal_sub, scopes, scheme}` for every tenant accumulates in one namespace outside
      the RLS boundary and outside the run's TTL. User message content already went there; a
      scope inventory is new.

- [x] **The Temporal path trusts the fiber row wholesale.** Documented beside the entry above:
      access to the namespace and the `felix-fibers` queue is treated like database access. `fiber_step` calls `advance_fiber`
      with the row straight from the workflow argument, never re-read from Postgres, and
      `_save_fiber` writes under `rls_bypass()`. Anyone who can start a workflow on the
      `felix-fibers` task queue therefore chooses `tenant_id`, `expires_at` and now
      `state["auth"]`. Temporal access is privileged; this should be a documented assumption.

- [x] **Memory tools are not untrusted.** Decided: untrusted by default — `memory` joins
      `_UNTRUSTED_SOURCE_PREFIXES`, so every screened manifest scans recall output (markers
      free; paid scoring per `model_tools`); `governed.yaml`, which names no tools, now screens
      it. As written: `recall` and `list_memories` are `transport: local`
      with `source: memory`, which is not in `_UNTRUSTED_SOURCE_PREFIXES`, so recall is not
      screened by default — `cowork.yaml` names them explicitly instead. Capture runs over turns
      containing untrusted tool output, so recall is a re-entry path for content quarantined on
      the way in. Either add `memory` to the untrusted prefixes or keep it a per-manifest choice.

- [x] **`scheme` replay on resume.** The sentence is in `deploy/GOVERNANCE.md`. A resumed fiber presents the recorded scheme without
      holding a credential, so `auth.inbound.schemes` can only ever agree with the enqueue-side
      check. Defence in depth lost, not a hole; worth a sentence in GOVERNANCE.md.

- [x] **`pr-quality-gate.sh` does not treat `durability/` as a control path.** Added, with a
      case in `tests/unit/test_pr_quality_gate_hook.py`. It reported
      "felix-security-reviewer is not needed" on #149, the most security-relevant change of the
      session — a resumed run's authority comes from there. Add `durability` to the token list.

- [x] **`durability` stays a closed `Literal`.** Fibers-vs-Temporal is not a factory swap, so a
      registry there is a feature, not a refactor. Recorded so it is not "opened" by mistake.
      Now `Literal["fibers"]`, kept only so `temporal` fails with a message rather than being
      ignored; a second backend would be a feature with its own design, not a registry entry.

#### Headless / contract

- [x] **Nothing enforces RLS coverage for a new tenant table** (readiness pass, 2026-09-04).
      `tests/unit/test_rls_coverage.py` renders every migration offline and checks the DDL:
      every `tenant_id` table carries `felix_tenant_isolation` and `FORCE`, every table has a
      `tenant_id` (allowlist: `memory_vector_config`), one Alembic head. `oauth_token_cache`,
      tenant-less and never read or written, is dropped in `0013` with its setting and helper.

- [x] **Headless invariant is prose only** — CLAUDE.md asserts it; nothing fails when it stops
      being true. An AST/file check over `apps/api` for `StaticFiles`, `Jinja2Templates` and
      `app.mount`, plus a tracked-file check for asset extensions, is ~20 lines in the existing
      idiom. Cheapest item here. Done 2026-10-02: two tests in `test_invariants.py`, an AST scan
      of every source root (`StaticFiles`, `Jinja2Templates`, `TemplateResponse`, any `.mount()`)
      and an asset-extension scan, each proven by a planted violation. The rule had in fact
      dropped out of CLAUDE.md entirely; it is back there and in the README.

- [x] **No-CORS contract undocumented** — the stack is body-limit → rate-limit → auth with no CORS
      layer, so a browser on another origin cannot call Felix directly. Deliberate, written down
      nowhere; the requirement survives only inside felix-web's `worker/index.ts`. A self-hoster
      pointing a browser app at `:8080` hits an opaque wall.
      (2026-09-29: still undocumented harness-side; the middleware stack is now request-id →
      security-headers → body-limit → rate-limit → auth, and the only statement is an aside in web
      `guide/terminal.mdx`.) Done 2026-10-02: README "API surfaces" states it with what a
      preflight actually gets (401, or 405 under `none`, no `Access-Control-*` headers, checked
      against the app) and the same-origin proxy pattern; CLAUDE.md's stack corrected; web
      `guide/deploy.mdx` says the same.

- [x] **Wire-contract snapshot** — `schemas/openapi.json` and `schemas/sse-events.json`, written
      by `scripts/gen-wire-contract.py` (`make contract`), held by `tests/unit/test_wire_contract.py`.
      The event names are scanned off the source — `Event(event=...)`, `{"event", "data"}` frames,
      `side_events.emit` — and a non-literal name must be a listed pass-through or the scan fails,
      so a new producer cannot slip past the snapshot. The OpenAPI snapshot drops `info.version`,
      which the per-release export keeps. Left for felix-run/web: generate its `StreamEvent`
      union from `sse-events.json` and close the open arm. **Snapshot-authoritative streaming**
      was not folded in.

- [x] **Publish the SDK, or say it is not one.** Decided: a light package, marked experimental.
      `felix/sdk.py` moved to `packages/client` (`felix-client`, module `felix_client`), httpx
      and nothing else, same API; `felix.sdk` re-exports it. The README says what it covers,
      that it is experimental, how to install it from the repository, and that the OpenAPI
      document on each release is the contract. Typed models, enums and full route coverage
      were deliberately not taken on — that is a compatibility promise, not a move.
      Found doing it: **the `felix_ai` → `felix` import boundary was enforced nowhere.** CLAUDE.md
      said `test_invariants.py` held it; an `import felix.config` planted in `felix_ai` left all
      2,996 unit tests and ruff green. `test_the_model_layer_and_the_client_import_nothing_of_felix`
      now walks every import node, lazy ones included, for both packages.

#### Control plane

- [x] **Edge posture behind a proxy and an IdP** (readiness pass, 2026-09-04). The rate-limit
      key read the *leftmost* `X-Forwarded-For` entry, the one the client wrote; a `tenant=claim`
      verifier with no `FELIX_ALLOWED_TENANTS` accepted any claimed tenant; `exp` had zero
      clock leeway; and a remote JWKS past its TTL 401ed every token from that issuer while
      `/ready` stayed green. Now: `FELIX_TRUSTED_PROXY_HOPS` counts from the right,
      `validate_runtime` refuses the unguarded claim mode outside development and colliding
      `tenant=issuer` verifiers everywhere, sixty seconds of leeway, and a `jwks` row on
      `/ready` that fails when no verifier is usable (refresh 300s, retry 30s).

- [x] **`Idempotency-Key` on `POST /chat`** (readiness pass, 2026-09-04) — one turn per key per
      tenant, Redis-backed claims across replicas, replay with `Idempotent-Replayed: true`,
      `FELIX_IDEMPOTENCY_TTL_SECONDS`.

- [x] **A tenant is a string.** Decided 2026-10-02: **that is the product.** Amended
      2026-10-05: a *person* can be a tenant — GitHub signup (`FELIX_GITHUB_SIGNUP=invite`) gives
      an invited account in no mapped org a personal tenant, `gh-<GitHub id>`, created by its first
      sign-in. Organisations remain configuration. `open` signup waits on a per-tenant spend cap.
      Tenants are the orgs
      sharing one install, and tenants and keys stay the install operator's configuration; an
      org's members get in through GitHub login or an IdP, but an org cannot self-administer its
      tenant or keys. Written into `deploy/GOVERNANCE.md#tenant-resolution`. The original note
      follows. There is no `Tenant` table and no `ApiKey` table; `tenant_id` is a
      column on every row and never a foreign key. Minting a key means editing
      `FELIX_AUTH_API_KEYS` JSON and restarting. Manifest CRUD, canary and rollback are real and
      API-driven; onboarding tenant #2 is a config edit and a process restart. Decide whether that
      is the product (single-operator self-host) or a gap, and write the answer down either way.

- [x] **GitHub login** (plan: device flow → self-issued JWT, org → tenant via
      `FELIX_GITHUB_ORG_TENANTS`). Partly answers the item above: a GitHub org is how a person
      reaches a tenant without a key edit and a restart. Three PRs, in order:
      - [x] 1. `felix/auth/github.py` (device flow, active-membership check, mint), settings,
        boot-time probe that every mapped tenant's token verifies.
      - [x] 2. `POST /auth/github/device` + `POST /auth/github/token`, public only when enabled;
        e2e through `create_application()`; `make contract`. From the PR 1 security review: a
        per-IP rate limit on both (a `slow_down` throttles the whole client id, so one abuser
        stalls every login); never log or audit a `device_code`; name the OAuth app plainly and
        document consent phishing (anyone can start a flow and ask a member to approve it).
      - [x] Device-start limiting protects a *shared* quota with a per-client key: one IPv6 /64
        or a botnet is many clients. Both done: `FELIX_GITHUB_DEVICE_STARTS_PER_HOUR_TOTAL`
        caps the deployment, and the device bucket keys IPv6 by /64 through an opt-in
        `client_key(..., ipv6_prefix=64)`. The global limiter still keys full addresses —
        throttling every user behind one /64 together there is a separate call, not made.
      - [x] Headless: a GitHub Actions OIDC exchange (`repository_owner_id` through the same
        org map) so CI gets short-lived tokens with no stored secret. `POST /auth/github/actions`;
        the repo allowlist became required (an org `actions` block names repositories, optional
        refs, and its own scopes), since an outside collaborator can run a workflow in a repo
        without being an org member.
      - [x] Client side of the Actions exchange: `felix_client.github_actions_login()` reading
        `ACTIONS_ID_TOKEN_REQUEST_URL`/`_TOKEN`, `felix login --github-actions`, and a felix-web
        CI recipe (`curl` first, since the CLI pulls in the harness).
      - [x] 3. `felix_client.github_device_login`, `felix login [--save]`, the REPL reads the saved
        token; felix-web auth guide (OAuth app setup, org approval, consent phishing).
      - [x] For a browser login in chat-ui: `GET /auth/methods` (public in every mode) reports
        `github_device` and `bearer_required` without starting a flow, and
        `POST /auth/github/token` returns `github_login`.

- [x] **Manifest version listing** — `GET /manifests/{name}/versions`: newest first, metadata
      only, each marked `active` / `canary`, paged by `before=<version>` (`next_before`).

- [x] **Run a job now** — `POST /jobs/{name}/run` (`jobs:write`), synchronous, returning the
      run. It goes through `scheduler.fire_job`, the path cron now uses too, so it runs as
      `cron` on the job's thread with its prompt screened; the run records `trigger: manual`
      and `requested_by`, and the schedule is left alone (`store.KEEP_SCHEDULE`).

#### Testing strategy

- [x] **Guard the structural scanners.** Fifteen scanners could pass by finding nothing:
      `_python_files()` and `_py_files()` answered `[]` for a directory that had moved, so a
      renamed package would have silenced the whole family at once. Both helpers now fail on a
      missing or empty root, and every scanner without a positive control gained one at its
      measured count — including the `httpx` timeout scanner the `test-quality` skill cites as
      having been green-on-broken once. `test_bash_guard_hooks.py` gained the
      `FELIX_REQUIRE_OPTIONAL_EXTRAS` hatch its sibling already had, and
      `test_resume_poll_backoff.py` now imports the symbol under test directly, so a rename
      fails collection instead of silently skipping the nineteen tests that carried the
      condition. Each guard proved by mutation, and two floors were corrected after review:
      one counted only non-exempt files, and both were re-derived from measurement.
      One overclaim surfaced while measuring: `test_every_record_usage_call_prices_by_the_wire_model`
      covers a single call site, not the eight its docstring named — `record_model_usage`
      absorbed the rest — and the docstring now says so.

- [x] **Decide what a steer queued on an idle thread should do.** Decided: **hold it for the
      next run**. It was accepted with 200, counted on the snapshot, then dropped — the loop
      drained steers only between tool rounds, so a turn that called no tool never read one,
      and `release_run_queue` discarded the in-process queue with it inside (on Redis it
      survived instead, and landed at some later run's tool round). Now the run drains held
      steers at its start, after its own turn, and clears the stale "cancel remaining tools"
      flag the steer raised, which would otherwise have cancelled that run's second tool call;
      `release_run_queue` keeps a queue that still holds undelivered messages, which also
      covers a steer arriving after a run's last drain. Refusing needed a cross-replica "is a
      run active" read and still lost the race at run end; promoting to a follow-up would have
      stopped the steer shaping the answer it was aimed at.

- [x] **`put_version` has a read-modify-write race on Postgres only.** It computed
      `SELECT coalesce(max(version),0)` then inserted, with no lock and no retry, so concurrent
      publishes of one name left one winner and `UniqueViolation`s — a 500 for a double-publish.
      The behaviour chosen: every publish lands, in order. A transaction-scoped advisory lock on
      `felix:manifest:{tenant}:{name}` serializes publishes of one name (the session store's
      append lock, same shape); other names never wait, and it releases at commit, so a
      transaction-mode pooler is fine. `test_concurrent_publishes_of_one_name_all_land_in_order`
      fires six at once on both arms and goes red on Postgres without the lock.

- [x] **An enforcing-RLS arm for the conformance suite.** Done
      (`tests/conformance/test_rls_enforcement.py`): a `NOSUPERUSER NOBYPASSRLS` role with
      `database_rls=True`, which is the configuration no other arm can reach — and the only
      one where a lost `rls_bypass()` is visible at all, since every other arm connects as the
      schema owner, where a bypass is a no-op. It guards one of the seventeen bypasses in the
      tree; the item below is the rest of them.

- [x] **`create_fiber` cannot insert under an enforcing RLS role.** Stale: both `create_fiber`
      and `get_fiber` open `tenant_session(settings, tenant_id)` since #201. As written: it was the one write in
      `durability/fibers.py` that neither wraps `rls_bypass()` nor binds the tenant GUC, so with
      `FELIX_DATABASE_RLS=true` and a non-superuser it fails with "new row violates row-level
      security policy". `get_fiber` has the same gap. Invisible to the conformance suite because
      that connects as a superuser with RLS off. Found while verifying the fiber claim contract
      against a live database; fixed in a separate change.

- [x] **Promote the ordering rule to a scanner.** Done: `tests/unit/test_ordering_rule.py` scans
      every module for an SQL `order_by` in a `.limit` chain and a Python sort whose result is
      sliced — assigned, sorted in place, or iterated in a sliced comprehension — and requires
      the last key to be a primary-key component (or, in a Python key, a PK column's name,
      `r["id"]` included). Sites keyed `file:function`; ties that are harmless are `EXEMPT`
      with a reason, and the three real ones are `KNOWN_OPEN`, a ratchet that fails when a fixed
      site stays listed. Floors per kind (14 SQL, 20 Python matched) and the three files named
      below must be reached — the first run of the mutations showed one shared floor let SQL
      matching break unnoticed. As written: it has now been fixed six times — the audit
      and usage cursors, `list_runs`, `list_jobs`'s collation, `list_active` twice — and two
      more shapes are still open below. The repo's own rule is that a lesson learned this often
      earns a structural gate rather than another round of review. The shape: over *any* module
      that truncates an ordered list, every `order_by(...)` and every `sort(key=...)` whose
      result is then cut must end on a primary-key component. Not `**/store.py` — that glob is
      what let `memory/recall.py` go unexamined through a survey written for exactly this
      defect, because its channels are hand-rolled `sorted(...)[:n]` rather than an `ORDER BY`.
      The site floor has to name `memory/recall.py`, `documents/store.py` and the session
      modules, or the gate will miss the seventh instance the way the survey missed the sixth.
      It must carry a floor
      on the number of ordering sites it matched, because a scanner that quietly stops matching
      is the failure mode this repo has already shipped once — an AST invariant here matched
      `timeout=<Constant>` while every literal it hunted lived inside `httpx.Timeout(...)`.
      Cheaper than catching the seventh instance in review, and it cannot be satisfied by a fake.

- [x] **`recall()`'s ordering defect, and its tiebreak that read a column nothing writes.**
      Done (`tests/conformance/test_memory_recall.py`, the first conformance arm this path has
      had). All three in-memory channels sorted on the score alone and all three SQL channels
      ordered on rank alone, so the candidate set entering fusion came from insertion order on
      one backend and the query plan on the other — and reciprocal-rank fusion scores on
      *position*, so that was amplified rather than absorbed. The ranking pass then tied again
      on score and recency. All six channels and the ranking now end on the id.

      `_rank`'s recency term read `last_used_at or created_at`, and `last_used_at` has no
      writer: the migration adds the column, `put_memory` sets it to None, the upsert excludes
      it. The dead half is gone and the docstring says "newest" means `created_at` — confirmed
      by mutation, since restoring the dead read changes no test.

      Still open, and deliberately not decided here: whether to write `last_used_at` on recall
      (a write on a read path) or drop the column in a revision. Until one of those, the column
      exists and nothing populates it.

- [x] **More listings whose two arms can disagree about order.** Closed: `list_approvals`,
      `list_plans` and `consolidate_pools` end on the id (tenant then id for the cross-tenant
      sweep), `collate(..., "C")` on Postgres so both arms compare bytes, each with a
      conformance case that goes red on either arm without it; `KNOWN_OPEN` in
      `tests/unit/test_ordering_rule.py` is empty. As written: Not one shape but three, and
      the first survey found only the first: a tie the twin breaks by insertion order and
      Postgres by nothing; a text key ordered by database collation on one arm and code point
      on the other; and the two arms sorting the same keys in *opposite directions*. The jobs
      contract found the first two and `list_jobs` now uses `COLLATE "C"`; the usage summary
      was the third, reversing `manifest_id` and `model_id` where the SQL ascended them, and
      is fixed with the jobs work because it already had a contract to assert it in.

      `memory/store.py`'s `list_active` (both sorts) and `as_of` are done, and so is
      `memory/recall.py` — see the item above. The remaining sites are `KNOWN_OPEN` in
      `tests/unit/test_ordering_rule.py` now, which fails when one is fixed and left listed.
      Remaining, ranked by what a wrong answer costs,
      and by function rather than line so the list stops rotting on every edit:
      `approvals/store.py`'s `list_approvals` (`created_at`,
      limited); `plans/store.py`'s `list_plans` (`updated_at`, limited); `eval/store.py`'s
      `list_runs` (`started_at`, unlimited, so ties only reorder). `approvals/store.py`'s
      `find_approved` already does it right — `decided_at`, `created_at`, then `id` — and is
      the pattern to copy.

      Approvals and memory have conformance files already, so those are cases to add rather
      than files to write; plans and eval are covered by the seam bullet above. Fix each with
      the arm that proves it rather than in one sweep.

- [x] **The audit and usage reads do not set the tenant GUC, so RLS empties them.** Wrong as
      written, and the real defect was the other half. Probed against a migrated database as a
      `NOSUPERUSER NOBYPASSRLS` role with `FELIX_DATABASE_RLS=true`: `/audit`, `/usage` and
      `/usage/summary` all return their rows, because `AuthMiddleware` wraps every request in
      a `RequestContext` and `_resolve_rls_tenant` falls back to its tenant; the worker's
      anomaly and continuous-eval sweeps bind theirs with `rls_tenant`. What failed was the
      **usage write**: `usage/store.py:_write_batch` bound no tenant, so the worker's
      `flush_usage` failed `WITH CHECK` on every tick, requeued, and failed again — the meter
      never reached Postgres on an RLS deployment and the buffer's ceiling dropped the oldest.
      It now binds per tenant, as the audit writer beside it always did, and
      `tests/conformance/test_rls_enforcement.py` flushes two tenants through the enforcing role.
      Every other worker task body was run against the same role and raised nothing.

- [x] **One unwritable audit event blocks every later one.** `flush_pending` requeued a failed
      batch whole, so an event Postgres would never accept was retried forever with every later
      event behind it. Closed by `DurableBuffer.drain`, shared by the audit and usage flushes: a
      failed batch is written again an event at a time, an event refused as data (`DataError`,
      `IntegrityError`) is quarantined — dropped, counted as `felix_buffer_quarantined`, alerted
      on, logged by id — and the first failure of any other kind stops the pass and requeues the
      rest, so an outage costs one extra round trip, not one per event. Found on the way: both
      writers commit per tenant, so a flush failing on a later tenant had already committed the
      earlier ones and its retry collided on the primary key — a poison event the flush made
      itself. Both inserts are now `ON CONFLICT DO NOTHING`, and the twins dedupe the same way.
      The trade: a *different* event reusing an id is now skipped rather than quarantined.
      Nothing in `packages` or `apps` supplies an id — `record_event` mints a uuid — so a
      collision can only be a retry; revisit if an id ever becomes caller-supplied.

- [x] **The keyset cursor's tie-break is collation-dependent.** Fixed with the other orderings:
      `keyset_order` and `keyset_before` both compare the id `COLLATE "C"`, so audit and usage
      page ties in byte order on both arms; a mixed-case conformance case pages one row at a
      time and goes red on Postgres with either half reverted. As written: `felix/cursors.py` pairs the
      timestamp with the row id, and `id` is text — so Postgres orders it by the database
      collation while the in-memory twin orders it by Python code point. The ids actually
      written are `uuid4().hex`, which sorts the same under every common collation, and
      `record_event` accepts a caller-supplied id only. Paging stays complete on both, since
      each backend is self-consistent; the exposure is the order of two rows in one
      millisecond differing between them, which no contract would catch because every test
      asserts set equality over pages. Fix if a caller-supplied id ever becomes ordinary.

- [x] **An index for the active-memory ordering's new tiebreak.** Done in migration `0020_ordering_indexes`, with approvals and plans too; `EXPLAIN` on 50k–200k seeded rows shows every listing as a plain index scan, where `0019` had an Incremental Sort (and a full sort for job runs and the claim). As written: `idx_memory_active` is
      `(tenant_id, manifest_id, status, created_at DESC)` and does not carry `id`, so the
      tiebreak adds an Incremental Sort over each `created_at` group. Bounded and cheap in the
      common case — but the case the tiebreak exists for is the batch write where one group is
      large (`consolidate_pools`, the memory writer), which is exactly when it is not. An index
      on `(tenant_id, manifest_id, status, created_at DESC, id DESC)` would make the
      unprioritised read a pure index scan, and would have to match the `COLLATE "C"`
      expression to be used at all. The prioritised branch leads on a `metadata`-derived trust
      expression no btree covers, so it benefits from none of this. Measured plan, not a guess.

- [x] **An index for the job run history's ordering.** Done in migration `0020_ordering_indexes`, with approvals and plans too; `EXPLAIN` on 50k–200k seeded rows shows every listing as a plain index scan, where `0019` had an Incremental Sort (and a full sort for job runs and the claim). As written: `list_runs` filters
      `(tenant_id, job_name)` and orders by `(started_at DESC, run_id DESC)`, while the only
      index on `job_runs` is the primary key `(tenant_id, job_name, run_id)` — so the ordering
      column is unindexed and the plan sorts a job's entire history before the `LIMIT` applies,
      unbounded in runs per job. A `(tenant_id, job_name, started_at DESC, run_id DESC)` index
      makes it a plain index scan. Read off the index set rather than measured, so measure
      first. Same family as the audit and usage item below, and one revision could carry all
      three.

- [x] **An index for the audit and usage listings' new ordering.** Done in migration `0020_ordering_indexes`, with approvals and plans too; `EXPLAIN` on 50k–200k seeded rows shows every listing as a plain index scan, where `0019` had an Incremental Sort (and a full sort for job runs and the claim). As written: Both now
      `ORDER BY ts DESC, id DESC` so the keyset cursor has a total order to page on, while
      `idx_audit_tenant_ts` and `idx_usage_tenant_ts` cover `(tenant_id, ts)` only. Measured at
      100k rows, 50 per distinct `ts`, the plan is an `Index Scan Backward` on that index under
      an `Incremental Sort` with `ts` presorted — correct, and cheap while a single
      `(tenant_id, ts)` group stays small, because only the group holding the page boundary is
      sorted. It degrades when one millisecond's group gets large. A `(tenant_id, ts DESC,
      id DESC)` index removes the sort node and makes the cursor a pure index seek; that is one
      revision, and a refinement rather than a correctness gap.

- [x] **An index for the fiber claim's ordering.** Done in migration `0020_ordering_indexes`, with approvals and plans too; `EXPLAIN` on 50k–200k seeded rows shows every listing as a plain index scan, where `0019` had an Incremental Sort (and a full sort for job runs and the claim). As written: `ORDER BY updated_at LIMIT 50` has no
      supporting index; measured at 200k rows it is 11 ms, and a partial index matching the
      claim's WHERE takes it to 0.15 ms at a fifth the size of `idx_fibers_due`. Worth doing now
      that the WHERE clause is stable.

- [x] **Worker cron bodies and CLI commands.** The eight cron bodies are covered and their
      schedules pinned (`tests/unit/test_worker_cron_tasks.py`), and every subcommand is now
      invoked (`tests/unit/test_cli_commands.py`, plus `eval` in
      `tests/unit/test_eval_gate_can_fail.py`). Running them found five defects, four of the
      same shape — exit 0, looks right, nothing downstream accepts it. `mint-jwt` wrapped its
      token at the console width so a captured one was rejected as `invalid_token`, accepted a
      `--tenant` the verifier refuses, and died on an unhandled `RuntimeError` with no signing
      key; `migrate` met `memory://` with a raw SQLAlchemy dialect traceback; and
      `temporal-worker` named its Postgres connections `felix-cli` for as long as it ran.
      `eval` printed its run dict through the same renderer, which splits any value longer
      than the console width mid-token — the fixtures are short so CI survived it, a real run
      would not.

#### Repo / release hygiene

- [x] **Credentials survive a `repr`.** Closed with `repr=False` rather than `SecretStr`: the
      value is untouched, so no call site changes, and what leaked was the rendering. Fourteen
      `Settings` fields — provider keys, `auth_api_keys`, `jwks_private`, the S3 keys, the
      signing secrets, the JSON blobs that carry credentials (`model_provider_options`,
      `webhook_endpoints`), and the URLs that can carry a password (`database_url`, `redis_url`,
      `warehouse_url`) — plus `HttpModelClient.api_key` and `extra_headers` (a gateway token can
      ride there). `tests/unit/test_credentials_out_of_repr.py` makes a new one fail closed: a
      field whose name looks like a credential must be hidden or listed as not one.

- [x] **Tag-driven release** — `release.yml` on `v*.*.*`: version verified against the tree,
      both images for both architectures to GHCR, Trivy on the pushed digest, SPDX SBOM attached
      and attested, cosign keyless signing, GitHub release from the changelog section.

- [x] **Single-source the version** — every workspace member's `pyproject.toml` and
      `__init__.py` plus `Chart.yaml` `version` + `appVersion` and `values.yaml` `image.tag`
      (more than the six this entry counted). `scripts/bump-version.py` is the list, a test
      proves it matches the tree, the release workflow refuses a tag that disagrees.

- [x] **`uv --exclude-newer`** — the CI lock check refuses anything published in the last 48h.

- [x] **CODEOWNERS** — `.github/CODEOWNERS` covers the controls, migrations, deploy and the
      supply chain; with branch protection's code-owner review, nothing on those paths merges
      without one.

- [x] **`.cursor/plans/` decision** — ignored and untracked (`40a0375`, `784213a`, 2026-08-22);
      this line predated its own review.

#### Product (`felix-run/web`)

- [x] **Rails before toast.** The last several PRs were all one toast component; `PRODUCT.md` says
      failure looks like "the rails are wallpaper". Cost view and eval instrumentation (C) are the
      two panels that make the right rail answer its own brief.
      (2026-09-29: the rail was redesigned so spend is read on the Ledger (`/harness`), which shows
      cost as a floor; still open: #345's per-eval-item cost and judge-fallback are not rendered by
      `eval-sheet.tsx`.) Done 2026-10-02 (web #310): the eval sheet shows a run's tokens and cost
      (a floor when an item was unpriced) and "judge fell back on N items", and each item its own
      duration, tokens and cost with a `heuristic` marker where the judge did not run.

- [x] **Labels name the thing, not the wire key** — `agent-sheet.tsx` says "Reply limit",
      "Conversation state" and "every turn replayed" (felix-run/web#139); raw values stay mono.

#### Deploy

- [x] **Cowork completion smoke on GCE** — the smoke's durable step polls
      `GET /chat/runs/{token}` to `completed` and asserts the reply (#375). A hard check rather than
      the soft one proposed: the worker now picks a fiber up within a second (#363) and runs it to
      suspension, so a completion takes seconds (3s on the first run). The old `noop smoke` prompt
      also left a pending `write_file` approval in production after every run; the prompt now asks
      for no tools.

- [x] **Governed demo path (decide)** — either enable on GCE (RBAC scopes for chat keys) **or**
      keep the demo anonymous and document that choice in `deploy/GOVERNANCE.md`. Decided
      2026-10-02: **anonymous**; written into `deploy/GOVERNANCE.md#example-agent`.

- [x] **Production in its own GCP project.** The VM moved from an unrelated project to
      `felix-507018` on 2026-10-03, by machine image. The Cloudflare Tunnel meant no DNS change,
      and the cutover took about 15 minutes. Secrets now live in that project's Secret Manager
      (`GITHUB_MCP_TOKEN`, `CLOUDFLARE_API_TOKEN` and the Anthropic key), so `contributor`,
      `triage` and `decider-support` compile in production. 0.6.1 is the first roll there.
      Open:
      - [ ] **`deploy/gcp/roll.sh` takes the project from gcloud's default.** It names the VM and
        zone but no project, so on a machine whose default is another project it reaches whatever
        VM of that name lives there. 0.6.1 was rolled with `CLOUDSDK_CORE_PROJECT=felix-507018` set
        by hand. Add `FELIX_GCP_PROJECT` beside `FELIX_VM`, and print the project in the preflight.
      - [ ] **Delete the old VM and its machine image** (`felix-api-cutover-20261003`). Both are
        kept for rollback. Never start that VM: it holds the same tunnel credentials, so Cloudflare
        would split traffic between two databases.

#### Later / explicit non-goals

- [x] **`memory.consolidate` LLM merge** — shipped as duplicate merging only (see "Memory
      consolidation" above); summarising facts into new text remains a non-goal.

- [x] **`memory.checkpointer` aliases** — resolved by implementing the field
      rather than deleting it. `postgres` / `none` are built in and the registry is
      open (`register_checkpointer`), so `agentcore` can be a plugin. `do` names
      Durable Objects and stays unimplementable here by invariant. There is no
      in-process built-in: a thread is not manifest-scoped, so a per-manifest
      *backend* would split-brain with the fifteen session routes.

### v0.12.0: durable runs on a production `cowork` thread (Oct 2026, #533–#536)

This started from one production `cowork` thread: write approvals stacked three deep, the
stream ended with `run_expired`, and after a reload the run looked dead. It was still working.
One audit found four harness gaps (#529–#532), and a client gap behind each, and all four
shipped together.

- **The symptoms were all one bug, seen from different sides.** The stacked approvals, the
  files written twice, and the user turns that landed between another run's tool calls all came
  from the second send starting a second run. Nothing refused it, because the fiber did not
  record its thread. `fibers.thread_id` and a per-thread advisory lock fixed it, and the
  `409 run_in_progress:<token>` reply also gave the client the handle it had been missing
  after a reload (#533).
- **"Expired" did not mean "stopped".** Expiry is checked only between steps, and a durable chat
  is one step. So the stream closed at 300s while the run went on, and `cowork`'s 600s
  approvals were then announced to no one. The client's poll also treated `expired` as not
  terminal, so it never ended. Two definitions of "over" had drifted apart. They now share one
  predicate, `run_in_flight` (#534).
- **A log written after the fact cannot explain a crash.** The tool-call message was written
  only after its batch returned. A run that died mid-batch therefore left no record of calls
  that may have happened, and the re-run asked the model again. Writing the message before the
  batch was enough for the next run to see those calls. It closes them as interrupted. It does
  not yet know which of them finished, because results are still written per batch (#535).
- **Fallbacks became one-way doors.** After one failed Redis read, a gated wait moved to an
  in-process future and never went back to Redis, while the API kept signalling through Redis.
  The lease heartbeat gave up after one failed renewal. Each fallback was reasonable alone, but
  together they turned a short Redis outage into an approval that timed out, or a step that
  kept running on a claim it no longer held. Waits now listen on both channels and also read
  the approval row (#535, #536).
- **The CI Redis is a dead port, and that helped.** The first version of the waiter fix stayed
  on Redis and hung the suite, which runs with `redis_url` pointed at port 9. A test setup
  that is always broken is cheap coverage for an outage, and here it caught a regression.
- **Shipped, and checked only by `/health`.** The scheduled smoke has failed since 2026-10-04,
  as the v0.9.0 entry records. So none of the new behaviour has been seen on production yet.
  It needs a person in `cowork` to reload mid-run, approve after 300s, and press Stop during a
  wait.

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
