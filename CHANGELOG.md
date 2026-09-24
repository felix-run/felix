# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A durable run's reasoning reaches a watching client while the run is going.** A durable
  stream carries no deltas, so the `session_event` frames tailed from the log are the only live
  view of the work, and they left out the assistant row's `metadata.thinking` — reasoning
  appeared only once the run had landed and the client re-read the snapshot. The frame now
  carries the readable blocks as `metadata.thinking: [{type: "thinking", thinking}]`, the key the
  snapshot already uses, so a client folds both with one function. Only readable text goes on
  the frame: a block's `signature` and any `redacted_thinking` exist to be replayed to the
  provider, and stay in the log. The key is omitted when there is nothing readable, and the
  reattach stream gets it too, since both go through `session_event_frame`.
- **`edit_file` changes one exact string and leaves the rest of the file where it was.** Until
  now the only way to change a file was `write_file`, which replaces the whole thing: the model
  reproduces every byte it is not editing, and the bytes it fails to reproduce are gone. That is
  how a stray docstring edit reached a Felix-authored branch. It also put `CHANGELOG.md` — 190 KiB
  now that entries are written in place — out of reach of any agent, since adding one paragraph
  meant sending the file back whole. `edit_file` takes `old_string` / `new_string`, refuses a
  match it finds twice unless `replace_all` says otherwise, refuses one it cannot find at all,
  and projects the resulting size before building it, so `replace_all` cannot inflate a file past
  the ceiling. It reads and writes bytes rather than text, because a universal-newline read would
  hand back `\n` for every `\r\n` and the write-back would rewrite every line ending in a CRLF
  file the edit never touched — the exact damage the tool exists to prevent. The write goes to a
  sibling temporary file and is renamed into place, carrying the mode across, so a write that
  fails partway leaves the original where it was: unlike a whole-file write, an edit's arguments
  do not carry the pre-image to retry from. `contributor.yaml` and `cowork.yaml` bind it — gated
  in `cowork` beside `write_file`, ungated in `contributor` where the builder container is the
  boundary.

- **A `policy_deny` audit row says which control refused the call.** `payload.control` is one of
  `policy`, `limits`, `guardrails`, `approvals`, `command`, `screening`. Every wrapper stamped its
  source on the deny it returned; the loop was the one reader that dropped it, so "show me every
  call blocked by approvals" was unanswerable from the audit log until now.

- **`make up-self` runs the Felix that builds Felix.** `deploy/docker/compose.self.yml` puts the
  api and worker on `Dockerfile.builder` — the lean image plus git, make and uv — with a separate
  clone of the repository at `/workspace` that the `contributor` and `triage` manifests edit and
  run the gates in. Two trees on purpose: the harness running the loop is the image's own venv;
  the harness being edited is the workspace. No Docker socket, no cloud credentials, one tenant —
  the host is the shell tool's boundary and this is that host.

- **Eval rubrics can score what a run did, not only what it said.** `tools_called`,
  `tools_not_called`, `max_tool_calls` and `max_errors` are read off the run's messages — and off
  `mock_tool_calls` / `mock_tool_errors` under `--mock`, so the counter-smoke can show each one
  rejecting. A run row now carries `error_count`, the subset of `fail_count` that never reached the
  scorer, so a malformed dataset reads differently from a model regression.
  `fixtures/eval/contributor.json` is the first dataset that scores the agent itself.

- **`felix-boundary` is the gate a Felix-authored pull request cannot edit its way past.** A
  `pull_request_target` workflow runs the base branch's `scripts/felix_boundary.py` — no head
  checkout — and fails a bot-authored PR that touches the files constraining Felix (`.github/`,
  `.claude/`, the governance and security packages, `builder.py`, the shell tool, migrations,
  deploy, its own manifests, this script), lacks the PR contract, closes a ticket no person marked
  `felix:go`, or changes paths the ticket did not name. A person's pull request passes without
  being looked at.

- **A scheduled job can start each firing on a fresh thread.** `payload.fresh_thread: true` on a
  `/jobs` row gives every run a thread of its own instead of the one thread per job name, so a job
  that works a different ticket each time does not carry ticket N's transcript into ticket N+1's
  context. The default is unchanged: a digest job keeps its history.

- **`spec.mcp_servers[].tools` allowlists which remote tools bind.** Glob patterns over the remote
  names; empty keeps the old behaviour of binding the whole catalogue. A non-empty list is what
  makes a server's mutating surface enumerable — an approval rule can only gate a tool it can name,
  and until now a write tool the server added overnight bound ungated with nothing to go red.
  `contributor.yaml` uses it, and its test now proves every bound GitHub write tool is gated
  instead of comparing the manifest to itself. A pattern the server no longer serves is logged at
  bind time, not refused.

- **Four `spec.plan_execute` fields now do what they say, and a fifth is gone.**
  `planner_model`, `executor_model`, `replan_on_failure` and `max_replans` each had exactly
  one reference in the tree: their own definition. A manifest could name a planner model, ask
  for replanning and cap it, and `felix validate-manifest` would bless all of it while the
  harness ignored every one — the only way to learn the truth was to grep the harness.

  `planner_model` and `executor_model` route the planning call and the subtask agent
  independently of the manifest's model. The asymmetry is the point of a plan/execute split:
  planning is one call whose quality shapes everything after it, execution is many narrow
  calls, so "plan with the expensive model, execute with the cheap one" is the lever the
  pattern exists to offer. Unset keeps the manifest's model, so nothing changes for a spec
  that says nothing. Everything else on the spec — price overrides, fallbacks, thinking level
  — is carried across: naming a planner route is not opting out of your own model
  configuration. `executor_model` is applied where `executor_recursion_limit` already
  was — in `_build_plan_execute`, the one place core builds this executor — which is worth
  saying because the first attempt applied it in `_DelegatingAgent` instead, on a branch that
  is dead for every compiled manifest, and left the field exactly as inert as before.

  `replan_on_failure` replans the **remaining** steps when one ends early, bounded by
  `max_replans` (`0` disables it as surely as the boolean does). The steps already done stay
  done and their notes are carried into the new plan, so a replan does not spend the budget
  twice. What counts as "ends early" is deliberately narrow — `refusal`, meaning governance
  replaced the reply, and `max_tokens`, meaning the model was cut off mid-answer. In both the
  note the synthesiser would record is not an answer. Everything else, including an empty
  reply, is a step that ran to completion and produced little: a planning problem rather than
  a failure, and replanning on it would loop on subtasks that are simply hard to say anything
  about.

  `planner_few_shots` was **removed** rather than wired. It named a count of planner examples
  with no corpus behind it anywhere in the tree, so there was nothing to make it mean, and
  inventing one to justify a field is the wrong way round. It is in `manifests/compat.py`'s
  `RETIRED` list, so a stored manifest that set it keeps loading — dropping it is inert
  precisely because nothing read it.

  `tests/unit/test_inert_manifest_fields.py` tracks this class of bug as a ratchet, and its
  list is four names shorter.

- **Felix has a program for building itself.** `docs/SELF.md` specifies the ladder — propose,
  confirm a ticket is ready, implement, open a pull request — with a person setting priority and merging
  at every rung, the evidence a ticket must cite, the Definition of Ready behind the new `felix_task`
  issue template, the pull-request contract, the self-modification boundary, the kill switches, and the
  scoreboard that graduates each rung. The manifests, shell tool, boundary check and scoreboard that
  carry it out land one pull request at a time behind this spec.

- **`scripts/self-scoreboard.py` prints the numbers that graduate a rung of Felix building Felix.**
  Read-only over `gh api` (and the builder's `/usage/summary` when pointed at one): evidence-cited
  issues, meta-work ratio, priority labels the bot applied, readiness at first check, verdict
  overrides, merge and rework rates, review rounds, PRs missing the contract — each beside the
  threshold `docs/SELF.md` sets. Nothing is posted; a person reads it and decides.

- **`spec.shell_tools` runs an allowlisted argv in the workspace checkout.** The capability the
  sandbox does not give a coding agent: `./scripts/test.sh`, `ruff`, `ty`, `git status` against the
  files `write_file` just edited. No shell interpreter — `&&` is an argument — and every prefix a
  manifest names must be covered by `FELIX_SHELL_ALLOWED_COMMANDS`, which is empty by default and
  checked at manifest write, at compile, and per call. The child sees five environment variables,
  `cwd` resolves under the workspace root, the run is killed at `timeout_ms`, and output is capped.

- **`manifests/triage.yaml` — Felix proposing work on Felix.** Rungs 0 and 1 of `docs/SELF.md`: it
  drafts evidence-backed tickets from failed smoke runs, issues people filed and roadmap items
  re-derived at HEAD, and scores open tickets against the Definition of Ready. It holds no file
  write, shell or sandbox; its only writes are issues and comments, each paused for a person.
  `skills/felix-self` carries the rules it works by.

### Changed

- **Changelog entries go straight into `CHANGELOG.md` again.** `changelog.d/` and
  `scripts/changelog.py` are gone; a pull request adds its entry under `[Unreleased]` in the
  section it belongs to. The conflict that directory existed to avoid is handled by git instead:
  `.gitattributes` gives the file `merge=union`, so two pull requests inserting there at once
  merge with both entries kept rather than stopping on a conflict a hand resolution can botch.
  The release step reads the section before cutting it, which is where an interleaved pair gets
  straightened out.

- **The loop's GitHub identity is `felix-run-bot`.** `felix-bot` was taken. The boundary check, the
  scoreboard and the builder's commit identity all name the new login; a pull request from the old
  name would now be judged as a person's, which is why the rename lands before the account exists.

- **`contributor.yaml` runs the gates it used to only name.** Version 2 binds the governed shell as
  `run` — the repository's gates and the git verbs that stay inside the checkout, no push, no `gh`,
  no bare python — and drops the one-shot Python sandbox. Approvals move from every file write to
  publication: `push_files` and `create_pull_request` pause for a person, and the approval row's
  arguments are the diff about to leave the machine. Anonymous callers are refused, as the shell
  tool requires. The prompt is rewritten around the contract in `docs/SELF.md`: one `felix:go`
  ticket, paste the gate output, open a draft, never merge.

### Removed

- **`spec.plan_execute.planner_few_shots` is gone.** It named a count of planner examples
  with no corpus behind it anywhere in the tree, so there was nothing that could make it mean
  anything — and inventing a set of examples to justify a field is the wrong way round. It was
  read by nothing, which is what makes dropping it inert.

  A stored manifest that sets it keeps loading: the field is listed in
  `manifests/compat.py:RETIRED`, so it is stripped on read with a warning naming it rather
  than failing the manifest under `extra=forbid`. Delete the line and re-save to clear the
  warning. Nothing else changes, because nothing ever read the value.

### Fixed

- **The contributor may rename and remove files in its checkout.** `write_file` only writes, so a
  misnamed file was permanent: the second rung-2 run created a changelog fragment under the wrong
  name and had no tool to fix it. `git mv` and `git rm` join the allowlist; both act inside the
  checkout, and a commit is what makes either matter.

- **The boundary check judges with the base branch's current script.** It checked out `base.sha`,
  which is the base as of the pull request's last synchronize, so a parser fix on `main` never reached
  an open Felix PR until its branch moved. It checks out `base.ref` now.

- **The boundary check reads the path, not the sentence.** A ticket's `Files expected to change`
  lines are written as `- `path` — why`, and the first Felix-authored pull request (#290) failed
  the surface check on every file because each whole line was read as a glob. The path is now the
  first backticked span or path-shaped token on the line; the rest is commentary.

- **The in-process waiter fallback no longer accumulates unbounded state.** When Redis is unavailable and `signal()` is called before `wait()`, the fallback stores a completed future in `_local` so the later wait can retrieve it. Without a bound, an authenticated caller POSTing `/chat/tool_result` with random `tool_call_id`s could grow the dict without limit. Signal-first entries are now capped at 1000; when the cap is reached, the oldest entry is evicted. A signal-first entry that is evicted before its wait arrives behaves identically to a signal that never happened, which is already the at-most-once contract the fallback provides.

- **The scoreboard no longer counts an "update branch" merge as a human commit.** The first Felix
  PR scored 0 % on "merged without human commits" because a person pressed GitHub's update-branch
  button, which authors a merge commit with no change of its own. Merge commits are skipped.

- **The builder image builds.** `.dockerignore` excluded `deploy/`, so `Dockerfile.builder`'s `COPY` of
  its own entrypoint failed with "not found" the first time anyone ran `make up-self`; the one file an
  image needs from `deploy/` is now let through.

- **A durable run no longer spends a scheduler tick per step, or a whole tick noticing it had
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

- **A react loop that runs out of steps says so.** It used to fall through with the model's own
  `tool_use` and a session status of complete, so a run cut off mid-thought looked finished — the first
  live triage run stopped at step ten with two tool calls pending and nothing recorded it. It now ends
  with `stop_reason: max_turns`, status `truncated`, and `felix_run_stop_reason{reason="max_turns"}`.
  Related: `spec.max_turns` never bounded a react agent — `spec.recursion_limit` does — and three
  bundled manifests carried it anyway; they set `recursion_limit` now, and the compile step warns when
  a single-agent manifest sets one without the other.

- **The self-build manifests can read a failed smoke run, and cache their prompts.** The GitHub MCP
  default catalogue carries no actions toolset, so `list_workflow_runs` and its siblings bound nothing
  — the first triage run logged exactly that. `triage` and `contributor` now bind from `/mcp/x/all`
  with the names that exist (`actions_list`, `actions_get`, `get_job_logs`); the allowlist narrows the
  rest away. `model.cache: true` on both: the first run paid $1.04 for ten calls with no cache reads.

- **The contributor may run `git show` and `git rev-parse`.** Both read-only; the first is how it
  compares its branch to `origin/main` before publishing (a repair run was refused three times without
  it), the second is how it reads the sha an evidence line names.

- **The shell tool's environment test passes inside the image.** CPython coerces a C locale to
  `LC_CTYPE=C.UTF-8` in the child (PEP 538), so the test that pins the five-variable environment
  failed in the builder container while passing on macOS and CI. It now tolerates that injection as
  it already did macOS's `__CF_USER_TEXT_ENCODING`. Found by Felix's first rung-2 run.

- **A streamed composite run now reports how it really ended.** The `done` event that
  `deep`, `router`, `parallel`, `groupchat`, `reflect` and `plan_execute` emit carried the
  final message and not the `stop_reason` beside it, so `/v1/chat/completions` — which fills
  `finish_reason` from exactly that field — reported the default for every streamed composite
  turn, including one the model truncated on `max_tokens` or the provider refused. The
  non-streaming path was always correct, which is why the gap survived: the same run answered
  honestly through `invoke` and vaguely through SSE. A reply-guard denial was the one case
  that already came out right, because `ReplyControlsAgent` rewrites the event it rewrote the
  reply on — and only a manifest with reply controls configured had that.

  The same field is read internally. `_pipe_stream` keeps the *last* terminal event it sees,
  so a composite delegating to another composite recorded `end_turn` however the child really
  ended — which meant `plan_execute` would replan a refused subtask on the `invoke` path and
  not on the streamed one.

- **The triage manifest names its repository and how to reach a line.** Two live runs taught it: one
  guessed the GitHub owner from the manifest's name and got 422 on every search; another spent all
  thirty steps paging a file by byte offsets to reach a line number that `search_files` would have
  returned in one call. The prompt now says both.

### Security

- **An eval `item_id` that cannot become a thread is refused at the write, not at the run.**
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

- **The image no longer ships a wheel's vendored copy of libpq's dependency chain.**
  `psycopg[binary]`'s manylinux wheels bundle their own builds of pcre2, krb5, ldap, sasl,
  selinux and OpenSSL, and declare them in an auditwheel SBOM — so a scanner reads, correctly,
  that the image carries RHEL 8's `pcre2 10.32` and **OpenSSL 1.1.1k**, long EOL. `apt-get
  upgrade` in the Dockerfile cannot reach inside a Python wheel, so those stayed at whatever
  version the wheel was built against no matter how current the base image was.

  It surfaced as a release failure that looked like flakiness and was not: 0.3.0's scan failed
  on `linux/arm64` with six pcre2 findings (three CRITICAL) while `linux/amd64` passed off the
  same Dockerfile and the same pinned base digest — the two wheels repair different library
  sets, and only the aarch64 one bundles pcre2. The Debian `libpcre2-8-0` in that same image
  was already patched.

  The image now installs `psycopg[c]`, which links the system libpq and vendors nothing: every
  one of those libraries becomes a Debian package that the existing `apt-get upgrade` already
  patches. The scan passes with **no suppression** — the findings went away because the library
  did.

  Deliberately a Dockerfile change and not a dependency change. `pyproject.toml` keeps
  `psycopg[binary]`, so `pip install felix-harness` and a contributor's `make install` still
  need no compiler and no libpq headers; only the image, which is the only artifact whose
  supply chain is a user's problem, pays the build cost. The builder stage gains `gcc`,
  `libc6-dev` and `libpq-dev`, none of which reach the runtime stage — it installs `libpq5`
  alone.

  The image got **smaller**: 577 MB against 587 MB, because the vendored libraries outweighed
  `libpq5`. Verified end to end rather than assumed — `psycopg.pq.__impl__` reports `c`, all
  sixteen migrations run to head against a real Postgres 17.11, and a query round-trips.

- **Every worker claimed fibers under the same name, so the lease-ownership guards decided
  nothing.** `durability/fibers.py` asks whether a claim is its own with
  `lease_owner == replica_id` — `_renew_lease` does it today, and it is what
  `_release_fiber` and `_record_attempt` rely on. `FELIX_REPLICA_ID` defaulted to the constant
  `"local"` and nothing ever set it: not the Helm chart, not a Compose overlay, and
  `validate_runtime()` did not ask for it under `scale_out`. The only mention of it in the tree
  was a commented-out line in `.env.example`.

  So with `worker.replicaCount: 2` every pod claimed as `"local"`, `WHERE lease_owner = 'local'`
  matched every claim including other pods', and `_renew_lease`'s own comment — *"a lease we
  already lost must not be stolen back mid-step"* — described a guard that did not hold between
  two replicas. It sat badly against the chart's own header on `deployment-worker.yaml`: *"Safe
  to scale: every task is lease- or lock-protected."*

  Two changes, because the default and the deployment are separate failures:

  * `replica_id` now defaults to `{hostname}:{pid}` — stable within a process, distinct across
    them, which is exactly the property the predicates need. Host and pid rather than a uuid
    because this is a value an operator reads: in Kubernetes the hostname is the pod name, so a
    lease row names the pod holding it. Docker Compose already gives each container a distinct
    hostname, so the `compose.replicas.yml` stack is fixed by the default alone.
  * The Helm chart sets it explicitly from the downward API (`metadata.name`), in the env tier
    every Felix deployment shares, so the identity does not depend on the container's hostname
    being meaningful and a new deployment template inherits it.

  The five places in `durability/fibers.py` that read the identity used to fall back to the
  literal `"local"` for a settings-like object without the field — recreating, in the one code
  path left, exactly the shared name this removes. They go through one helper now, whose
  fallback is the same per-process rule.

  An empty `FELIX_REPLICA_ID` is now refused rather than silently defaulted. It would be worse
  than the constant it replaces: `lease_owner` is `""` on every *unclaimed* row, so an empty id
  would match every released claim as this worker's own.

  **Nothing was broken end to end before this** — claim exclusion rests on `lease_until` plus
  `FOR UPDATE SKIP LOCKED`, which never depended on the identity. What was missing was the
  second line of defence those three predicates are written to provide. Found by the security
  review on felix#262.

## [0.3.0] — 2026-09-16

### Added

- **An approval now says why it fired and what it is blocking, on both channels.** `GET
  /approvals` rows carry `reason` and `tool_call_id` (migration
  `0015_approval_reason_and_call`), and the `approval_required` stream frame carries
  `expires_at`. Until now the two channels were each missing what the other had, and both
  sides of the wire had written that down: `manifests/builder.py` at the emit noted that the
  rule's `description` "reached no client by any route: the `/approvals` row does not carry it
  either", and `@felix/client`'s `PendingApproval` documents `reason` as **frame-only** ("an
  approval the poll found has none to show") and `expiresAt` as poll-only ("the frame carries
  no deadline").

  That asymmetry falls hardest on the path with no choice. A **durable** run's agent is in the
  worker while its stream is served by the API, so no side event can cross and the poll is the
  whole channel — and the poll was the half that could not say *why* a gate fired. An operator
  who found a waiting approval was shown a tool name and a rule id, while `description` — the
  one field in `ApprovalRule` written to be read by a person — sat unread. `tool_call_id` is
  the same shape of gap: it is what attaches the prompt to the tool card it is blocking, so
  without it a polled approval floats free of anything on screen.

  `expires_at` on the frame is read off the row rather than recomputed, so the two channels
  cannot disagree about when an offer lapses. Null there means the rule set no `ttl_seconds`,
  which is a real state a client renders from its own default rather than a missing value.

  **`GET /approvals?thread_id=…`** narrows to one conversation, applied in SQL before `LIMIT` so a
  busy tenant cannot hide the thread you asked about — filtering a returned page client-side would
  drop whatever the page had already cut off. `?thread_id=` (empty) means "approvals with no thread"
  and is distinct from omitting the parameter. `FelixClient.list_approvals` takes the same argument.

  It **under-reports by construction**:
  `create_pending` reuses a pending row keyed on (tenant, manifest, tool, call signature), so
  the row names whichever thread asked first and a second thread blocked on the same reused row
  is not listed under its own id. That is the safe direction — a caller asking about one thread
  never learns about another's — and it is why `thread_id` is attribution rather than ownership.

  Expand-only, empty on every historical row, and a widening for clients: `''` is the harness
  saying it has no answer (a command-screening gate has no rule description; a gated tool
  called outside a tool loop has no call id) rather than a missing value, the same choice
  `rule_id` and `thread_id` already made. Clients mirroring the wire should add all three as
  **optional**, so they keep working against a harness that predates them.

- **`approvals` can now be reclaimed, and `jobs/retention.py` stops claiming it already was.**
  That module's docstring listed the table among those "bounded by something else… `approvals`
  and `job_runs` go with their run or job". For `approvals` no such binding exists: no foreign
  key, no cascade, and nothing anywhere issued a delete against it, so every gate firing left a
  row that outlived its run by the life of the deployment. Worse, nothing moves a *timed-out*
  row off `pending` — `wait_for_decision` returns a denial and the caller writes nothing back —
  so the set a thread-scoped query walks grew monotonically and forever.

  `FELIX_APPROVAL_RETENTION_DAYS` (default **0**, keep) sweeps **settled** approvals older than
  the TTL, on both backends, under the nightly job.

  **Settled means the row can no longer authorize a call**, and that distinction is the whole
  design. An approval row is a *permission*: deleting one that could still be returned by
  `find_approved` would revoke it silently, from a cron job, days after an operator granted it —
  experienced as a tool that used to work and now denies, with nothing in the audit trail saying
  why. So the rule is the exact negation of `find_approved`'s own predicate:

  - swept: anything not `approved`, plus an `approved` grant whose `expires_at` has passed
  - never swept: an unexpired `approved` grant, however old — and one with a **null**
    `expires_at`, because the rule set no `ttl_seconds` and that grant is standing by
    construction

  `consumed_at` is deliberately not part of the test. `one_shot` lives on the manifest rule
  rather than the row, so a consumed grant still authorizes a tool whose rule is not one-shot.

  **Off by default**, matching `FELIX_SESSION_RETENTION_DAYS`: the row is the record of a human
  decision on a gated tool, and the `soc2` and `eu_ai_act` profiles lean on it, so an operator
  opts in rather than out. Turning it on is also what bounds the pending-row accumulation that
  made a durable run's thread-scoped approval query walk a growing set.

- **An approval row now names the conversation it is blocking.** `GET /approvals` (and
  `GET /approvals/{id}`) carry `thread_id`, which the `approval_required` stream frame has
  carried all along. The two channels do not cover the same runs: side events are an
  in-process queue keyed by thread, so a **durable** run — agent in the worker, stream served
  by the API — can only be seen through the poll, and that was the half with no thread on it.
  An operator opening a tab cold could be told that something was waiting but not what.

  It is the **originating** thread, not an owner. `create_pending` reuses a pending row keyed
  on (tenant, manifest, tool, call signature), so two threads issuing a byte-identical gated
  call still share one row and one decision; the field names whichever asked first and a later
  thread does not overwrite it. Treat it as attribution — a hint good enough to link to, not a
  claim that exactly one conversation is waiting. Widening the reuse key to make it exact would
  change grant scope, which is a product decision rather than a serialization fix.

  Empty where there is no thread — a gated tool called outside a chat context — the same way
  `rule_id` is empty when no rule named it. Historical rows read `""`, since they genuinely
  have no answer; `migrations/versions/0014_approval_thread_id.py` is expand-only with no
  backfill. Clients mirroring the wire should add it as **optional**, so they keep working
  against a harness that predates it.

  One case never reads empty, and it is the motivating one: a **durable** run started without a
  thread gets `{tenant}:fiber:{fiber_id}` synthesized for it, which is a real session thread and
  openable like any other. So the poll can attribute a durable approval even when the caller
  never opened a conversation.

- **Uploads now have a per-tenant ceiling and a retention sweep.** `MAX_ATTACHMENT_BYTES`
  capped one upload and nothing capped how many — the same gap that produced
  `documents_max_per_tenant`, because a per-request cap is not a per-tenant cap. The security
  review of `/files` named this as the condition on granting `files:write` to an untrusted
  tenant, and it was shipped twice without it, written into the roadmap rather than assumed.

  `FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` (256 MiB, `0` disables) bounds what one tenant may
  store. Bytes rather than a count, because the resource being protected is disk — and with
  each upload already capped at 600 KiB, a count ceiling is just this number divided by that
  one with worse failure text. An upload over the line answers **409**, not 413: the request
  is a fine size and the account is full, and a 413 sends the caller off to shrink an image
  that was never the problem. It is checked before the object is written, so a refusal leaves
  nothing behind.

  `FELIX_ATTACHMENT_RETENTION_DAYS` (`0`, keep forever) lets the nightly sweep collect old
  uploads. `attachments/` had joined `artifacts/` as an object-store prefix nothing ever
  collected, so on the default `fs` backend a tenant's uploads accumulated against the same
  disk that holds artifact spill and manifest storage. The default stays "keep forever":
  deleting caller data on a timer is an operator's decision, and a thread can reference an
  attachment long after it was uploaded.

  Both need something the object store cannot give them. The `ObjectStore` Protocol has no
  `list` — deliberately, since S3 and GCS charge for it and the filesystem backend would walk
  a directory tree per request — so nothing could count what a tenant had stored or find what
  was old enough to drop. Migration `0016` adds an `attachments` ledger beside the bytes,
  with the same tenant RLS policy as every other tenant table.

  The bytes remain the system of record and the ledger can drift from them, so the ordering is
  chosen rather than incidental: the row is written **before** the object and deleted **after**
  it. Every way this can be interrupted therefore leaves the same shape — a row whose bytes may
  not exist — which over-counts, is visible to an operator, and is collected by the sweep. The
  opposite order is unrecoverable in both directions: bytes with no row are invisible to the
  count *and* to the sweep, because both read rows, and it would also make the quota fail open,
  since a total that never grows never refuses anything. Existing uploads are not backfilled, because a backfill would have to
  list the object store, which is the operation this table exists because we lack; they are
  invisible to the quota and the sweep, exactly as they were before.

- **A file can be uploaded once and referenced by id.** `POST /files` stores bytes in the
  object store and returns a `file_id`; `GET /files/{file_id}` reads one back. Both are
  scope-gated on `files:write` / `files:read`, separate from `artifacts:read` because that
  reads spill the *harness* wrote while these are caller-supplied bytes with a caller-driven
  lifecycle.

  The point is not convenience. An image sent inline as a `data:` URL lands in the session
  event log, and `full_replay` sends that log again on every subsequent turn — so a 600 KiB
  screenshot attached on turn one is re-uploaded to the model on turns two, three and four.
  The 1 MiB body limit bounds each *request* and nothing bounds the thread. A reference is
  small enough to replay.

  Shaped on `/artifacts`, and for its reasons: the tenant comes from the caller's credentials
  and is never a path segment, so no spelling of a reference reaches another tenant's upload;
  a malformed reference answers 404 rather than 400, because which ids are well-formed is not
  a caller's business; and the id is a uuid4 hex generated by the server, since a
  caller-chosen id is a caller-chosen object key.

  Uploads are capped at 600 KiB decoded and restricted to the image media types both wires can
  actually encode. The cap sits *below* the 1 MiB body limit on purpose — above it, the
  middleware answers 413 before the route is reached and the caller is told the request was too
  large without being told the real ceiling — with extra margin because base64 in a JSON
  envelope inflates by a third.

  **Not yet wired into a conversation.** Resolving a `file_id` in a message back to bytes
  belongs at the wire and is the follow-up; resolving late is what keeps the session log
  holding the reference rather than the base64 it expands to.

- **Changelog entries are files now, so two pull requests cannot conflict over one.** Every
  change adds `changelog.d/<section>-<slug>.md` instead of appending to `CHANGELOG.md`, and
  `python3 scripts/changelog.py --release X.Y.Z` folds them in when the release is cut.

  Appending to the top of one block meant any two open pull requests collided there, every
  time. That conflict is worse than most: the resolution is prose, no merge tool helps, and a
  botched one silently drops somebody's entry — which happened, six at once, to a rewrite that
  should have been a merge. Two fragments are never in the same file.

- **`spec.output_schema` now works on `router`, `parallel`, `reflect` and `plan_execute`.**
  It shipped supporting only the single-agent patterns, and `build_agent` refused the
  combination outright for the composites rather than accept a manifest that declares an
  answer contract and returns free text.

  The reason it was refused is the reason it is interesting: a composite reaches a model
  several times per run — routing, planning, critiquing, scoring, synthesizing — and exactly
  one of those turns produces what the caller receives. So the contract is placed per
  pattern, never applied wholesale:

  - **`parallel`** shapes the synthesis. The specialists stay free-form; their answers are
    raw material for the aggregator's prompt, not the reply.
  - **`plan_execute`** shapes the final synthesis. The planning turn and each executor step
    stay free-form — a plan shaped like the answer schema is not a plan, and a subtask answer
    shaped like it arrives as a JSON envelope in a notes list the synthesis reads as prose.
    That took stripping `output_schema` from the context the executor is built from, not just
    withholding it at the call site: `build_react_agent` reads the schema off the build
    context onto the agent itself, so an executor built from the shared context carried it
    regardless of what it was handed per turn.
  - **`router`** shapes the child it routes to. The classifier turn does not: a router that
    replied with its classifier's JSON would satisfy the schema and answer nothing.
  - **`reflect`** shapes every draft, because the loop exits as soon as one clears the
    threshold and "the last iteration" is not knowable in advance.

  `groupchat` stays refused, and the refusal now carries its reason: its answer is the last
  speaker's message *stamped with its name* (`[researcher] …`), so even a child returning
  perfect JSON comes back with a prefix in front of it. Supporting it means dropping the stamp
  — losing who spoke, which is the pattern's point — or adding a synthesis turn it does not
  have.

  `_child_input` also stopped dropping `model_options`, so a caller's `/v1` `response_format`
  reaches a composite's answering turn — and a child — for the first time. Where a manifest and
  a request both specify a schema the **manifest** wins, matching `react._chat_options`: an
  agent published with an answer contract keeps answering to it rather than to whichever shape
  the last request preferred.

  A composite that declares no schema is unchanged, deliberately including the case where the
  request carries other options. `_DelegatingAgent` has no `limits`, so unlike `react` it
  cannot clamp `max_tokens` to `limits.max_output_tokens` — forwarding a request's options to
  the synthesis turn would let `max_tokens: 200000` size the turn that composes the answer on a
  manifest capping output at 2000.

- **A durable run now says what it is waiting for you to approve.** `POST /chat/stream` on a
  manifest with `spec.execution.mode: durable` emits `approval_required` frames for the gates
  the run is blocked on, alongside the `run_status` and `session_event` frames it already
  carried. Previously it emitted neither: the agent runs in the worker while the stream is
  served by the API, so the in-process side event could not cross, and `GET /approvals` was the
  only channel — on precisely the path where a human has time to answer, because the mode exists
  for work nobody is watching.

  **Rebuilt from the approvals row, not forwarded across a bus**, which is the whole design. A
  pub/sub bridge would be at-most-once, and here the message *is* the prompt: drop it and the run
  blocks its entire `ttl_seconds` before denying, with nobody ever asked. Reading the durable row
  instead keeps the property the session-log tail relies on — a missed poll costs latency, never
  a decision — and it means a client that attaches *after* a gate fired still sees it, which a
  bus cannot offer. This is what the preceding change (`reason` and `tool_call_id` on the row)
  was for: every field the frame carries is now on the row, so it can be re-derived.

  The frame is the same eight keys the transient path emits, so a client folds it with the
  handler it already has.

  **The frames require the `approvals:read` scope**, the same one `GET /approvals` requires; a
  caller without it gets the transcript, the status and the answer exactly as before, and nothing
  about gates. That is not caution for its own sake — `thread_id` comes from the request body, so
  the thread a durable run names is a question the caller *chose* rather than one they own, and
  nothing in Felix binds a thread to a principal. Ungated, chat access alone would have read the
  tool names, arguments and gate reasons of any thread in the tenant. `admin` bypasses and
  `approvals:write` implies `approvals:read`, as on the route, because both ask one function.

  Two more behaviours worth knowing. **Each approval is announced once per stream** — `GET
  /approvals` answers "what is pending now", so the row returns on every poll until it is decided,
  and re-showing a prompt someone has already answered is worse than showing it late; a decided
  **or expired** gate is never announced, since both are history rather than a question. And the
  frames are **thread-scoped**: a run only ever announces gates attributed to its own thread.

  **`GET /approvals` remains the channel of record.** A stream that was never opened, or that
  dropped before a gate fired, sees nothing — the frames are a convenience for a watching client,
  not a replacement for the poll. An operator console should still poll.

- **Eval dataset items are validated instead of silently stored empty.**
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

- **A turn can now name an uploaded file instead of carrying it.** `POST /files` stored bytes
  and handed back a `file_id`; nothing consumed one. A message may now include OpenAI's own
  `{"type": "file", "file": {"file_id": "…"}}` content part, and the harness expands it to the
  stored bytes immediately before the model call.

  The expansion is late on purpose, and that timing is the whole feature. An image sent inline
  lands in the session event log, and `full_replay` re-sends that log on every later turn — so
  a 600 KiB screenshot attached on turn one is re-uploaded to the model on turns two, three
  and four. Expanding at ingest would have put the base64 in the log and paid that cost
  silently. The log keeps the reference; the bytes are fetched per turn.

  The reference rides in the same `url` field an inline image uses, as a `felix-file://` URI,
  rather than in a new field. The session layer already persists and restores `attachments[].url`
  and both wires already read it — a parallel `file_id` field would have had to be threaded
  through each of those, and the one that would have been missed is the session layer, which is
  the only path a *second* turn takes. Nothing would have failed until replay.

  The tenant comes from the caller's credentials and never from the reference, so naming
  another tenant's `file_id` is indistinguishable from naming one that never existed. The media
  type is read back off the bytes rather than from a stored label, because the default
  filesystem store discards the label — and the sniff now checks a RIFF container's format
  tag, since `RIFF` alone is also WAV and AVI and this is the only thing deciding what a model
  is told these bytes are.

  A reference that cannot be resolved — deleted since, another tenant's, or bytes that are no
  longer a type any wire encodes — is dropped with a warning naming the id rather than raised:
  the turn naming it is already in an append-only log, so refusing would make a thread
  unanswerable for good the moment an attachment was deleted. Where dropping would leave the
  turn with nothing to send, a short marker replaces it instead. A turn whose only content part
  was the reference would otherwise reach the provider with empty content, which is itself an
  error and would wedge the thread just as permanently by the other road; the marker also stops
  the model answering confidently about an image it was never shown. The id appears in that
  marker only when it is well formed, because that text reaches the model.

  The same id repeated across a replayed context is read once per model call.

  Unchanged, and worth stating because the roadmap left it open: resolution happens **after**
  `apply_inbound_screening`, which is where it has to be if the log is to keep the reference.
  That is not a regression in screening coverage — `_message_text` collects only blocks of type
  `text`, so image content has never reached a screener, inline `data:` URLs included. Text
  rendered inside an uploaded image remains an injection channel that screening does not see,
  exactly as it was for inline images.

- **Structured output: `spec.output_schema`.** A JSON Schema the agent's answer must match,
  enforced by the model provider rather than asked for in the prompt. `message.content` is then a
  JSON document on every provider — `response_format` on the OpenAI wire (strict where the schema
  closes every object and requires every property, which is the only setting under which the shape
  is guaranteed), and on Anthropic, which has no equivalent, a tool the model is required to call,
  folded back into the reply so a caller sees the same document either way. Tools still work: it is
  the turn that answers in text that is constrained, not the turns that call a tool on the way.

  `POST /v1/chat/completions` accepts OpenAI's `response_format` for the same thing per request, so
  an OpenAI SDK works unchanged. A manifest that declares `spec.output_schema` overrides it — an
  agent published with an answer contract keeps answering to it rather than to whichever shape the
  last caller preferred.

  Supported on `pattern: react` and `pattern: deep`. The composite patterns compose their answer in
  a turn that takes no per-request options yet, so a manifest declaring `output_schema` on one of
  those is refused at compile rather than quietly answering in prose; a plugin's pattern opts in
  with `register_pattern(..., honours_output_schema=True)`.

- **A manifest can now say its declared skills are the whole set.**
  `spec.skills_declared_only: true` loads only the names in `spec.skills`; anything else on
  the host stays out of the catalogue and out of the model's prompt.

  Without it — the default, and unchanged — `load_manifest_skills` seeds every skill in the
  bundled directory and in `FELIX_SKILLS_DIR` before it resolves a single ref, so `spec.skills`
  adds to a host-wide library rather than restricting one. A manifest declaring one skill
  compiles a catalogue holding every skill on the host, and all of them are offered to the
  model. That was surfaced by the `/skills` routes and recorded as a question rather than
  fixed, because it is a reasonable design and it is what every stored manifest was written
  against.

  It is worth being able to turn off because a skill body is appended to the system prompt.
  An ambient skill is therefore a prompt fragment the manifest never named — the one
  prompt-shaping input `pin_compile` cannot cover, since the hash is over the manifest while
  the drift is on the host's disk. A manifest that has to be reviewable can now enumerate
  every instruction its agent may load; one using the host as a shared library carries on as
  before.

  **Opt-in on purpose.** Narrowing by default would change behaviour for every manifest
  already in Postgres, which this repo's own rule says needs a migration rather than a
  reinterpretation — and a stored manifest relying on an ambient skill would start answering
  differently with nothing in its own text having changed.

  `GET /skills/{manifest}` reports `declared_only` alongside the per-skill `declared` flag, so
  the two readings are distinguishable from outside. Restricting does not break resolution: a
  declared name still resolves against the bundled directory, so it keeps its body rather than
  degrading to an empty placeholder.

  Separately, `spec.skills` gained the `max_length` every other ref list already had. Each ref
  can cost an object-store lookup at compile, so an unbounded list was an unbounded fan-out.
  Naming it plainly, because this repo's own note says a narrowed field has no compat
  mechanism the way a removed one does: a stored manifest with more than 64 skill refs stops
  validating, and since the store is read ahead of bundled YAML it goes dead rather than
  falling back. No such manifest is plausible at that bound, but the precedent should be
  visible rather than rediscovered.

  **Upgrading.** Adding a field to `spec` used to change every manifest's content hash, which
  would have drifted pinned threads and failed in-flight durable fibers. That is fixed in the
  same release — see *Adding a field to the manifest schema no longer breaks every pinned
  thread…* under **Fixed** — so the hash rotates **once** for this release as a whole rather
  than once per change. The operator action is stated there; do not do it twice.

  One thing specific to this entry: `manifests/governed.yaml` sets `pin_compile: true` *and*
  is edited here, so threads pinned to it drift on their own account as well. The README
  already records that editing a bundled manifest is drift by design.

- **Agent Skills are reachable over HTTP.** `grep -rn skill apps/api/src/felix_api/routes/`
  returned zero: a loader, a catalog, an activation store with its own table and Postgres arm,
  and three model-facing tools — none of it answerable to an operator without opening psql. A
  surface nothing can reach is inert by this repo's own rule, and skills were the largest
  built-but-unreachable subsystem left.

  `GET /skills/{manifest}` lists what the manifest can reach and which are active.
  `GET /skills/{manifest}/{skill}` returns one, including the body `activate_skill` would hand
  the model — that body is appended to the system prompt, so it is prompt content an operator
  is accountable for and could not otherwise read. `GET /skills/{manifest}/activations/recent`
  says which skill activated on which turn. All three gate on a new `skills:read` scope, kept
  separate from `manifests:read` because reading a skill body is reading instructions the
  agent will follow, which is a different question from reading the manifest that names it.

  Read-only, deliberately: activation is a decision the model makes mid-turn, and the store is
  keyed by `(tenant, manifest)` rather than by thread, so an operator toggling from outside
  would be writing shared state a run is concurrently reading with no turn to attribute it to.

  Two things the routes surface that nothing surfaced before, both found while building them:

  - **`spec.skills` adds to a host-wide library rather than restricting one.**
    `load_manifest_skills` seeds every skill in the bundled directory and in
    `FELIX_SKILLS_DIR` before it resolves a single ref, so a manifest declaring one skill
    compiles a catalog holding every skill on the host — and all of them are offered to the
    model. Verified directly: a manifest naming one skill reached seven, including the repo's
    own `felix-architecture` and `felix-contributing`. This may well be the intent, but it is
    not what "declared skills" reads like and nothing said so anywhere. The new `declared`
    field on each item is how the difference becomes legible; the behaviour itself is
    unchanged here and recorded in the roadmap as a question.
  - **The audit trail recorded that a skill activated but never which one.** `tool_runner`
    already emits a `tool_call` event for every tool, and its payload carries the tool's name
    and not its arguments — deliberately, since arguments are arbitrary model text and a
    credential in a retained row is how that goes wrong. `skills/tools.py` now emits a
    `skill_activation` event naming the skill, which is safe to record precisely because
    `activate` resolves it against the catalog first: the stored value is a name the host
    declared rather than anything the model typed. A model naming a skill that does not exist
    is recorded too, under its own status, because that is itself worth seeing. Both tools
    resolve first: `deactivate` originally did not, and `activation_store.deactivate` is a list
    filter that neither validates a name nor reports whether anything was removed — so every
    call succeeded and arbitrary model text went into a retained row under `status="ok"`,
    indistinguishable from a real deactivation. Both reviewers found it; the security review
    ranked it the highest finding in the change.

  A skill body is redacted before it leaves, for the reason `routes/manifests.py` redacts a
  manifest on `manifests:read`: this is the lower scope and an embedded credential must not
  ride out on it. Nothing validates SKILL.md frontmatter, and the same bytes reaching the
  *model* are already masked by the governance stack — so without this the HTTP route would
  have been the only path on which a skill body reached anyone unmasked. The absolute
  `Skill.path` is reduced to a basename for the same reason: it is derived from the install
  prefix, so returning it told a tenant-scoped caller the container's filesystem layout.

  `audit_store.query` gained an optional `manifest_id` filter. The route first over-fetched a
  fixed window and narrowed in Python, which is wrong in a way the caller cannot detect: a
  tenant whose activations on one manifest exceeded the window got an empty list for another,
  identical on the wire to "that manifest has never activated a skill" — and a model calling
  `deactivate_skill` in a loop could push a real activation out of view, which is
  anti-forensics against the one question the route exists to answer. The filter is four
  lines mirroring the `event_type` and `status` filters already there, and adds no clause for
  any caller that does not pass it.

  Also: `felix.skills.store.clear_memory()`, the test seam every other `memory://` store
  already had, wired into the conftest registry that exists to call these. Without it one
  test's activation is the next test's starting state, which is how a tenant-isolation
  assertion passes alone and fails in a file.

- **`POST /v1/chat/completions` accepts an image.** `content` was typed `str | None` on that
  surface, so a multimodal request was a 422 before any of the request ran — on the one endpoint
  whose stated purpose is that an OpenAI SDK works unchanged, while `/chat` accepted the same
  message and both wires knew how to encode it. It now takes OpenAI's list of content parts as
  well as a plain string. The parts stay untyped dictionaries on purpose: the shape is OpenAI's
  and it grows, and what Felix does with a part it does not recognise is decided in one place, by
  the message validator, rather than by a model that would reject next year's part type.

### Changed

- **`GET /usage/summary` builds its rows through a named serializer.** The response was
  assembled inline in both arms of `felix.usage.store`, which made the shape unreadable to
  anything outside the process: `felix-web`'s payload guard reads `_<row>_dict` functions to
  learn what a route actually sends, so this area could not be guarded at all and a client
  type naming a field the harness never sends would have typechecked, linted and rendered a
  blank forever. It is the gap `/documents` had before #213.

  `_summary_item_dict` and `_summary_totals_dict` now own that shape, and the memory and
  Postgres arms both return through the first of them rather than agreeing by inspection —
  those two have disagreed before, about the order of rows sharing a day. No wire change: the
  keys, their types and the rounding are what they were.

### Fixed

- **A durable run now reports what it is doing, not only that it is running.** `POST
  /chat/stream` against a manifest with `spec.execution.mode: durable` carried
  `run_accepted` → `run_status` → `final` and nothing else, so the answer arrived and the
  tool calls behind it did not — a client drawing tool cards showed a bare reply until the
  thread was next hydrated. The stream now interleaves the thread's **session log** between
  status frames, as `session_event` frames with the same `id:` cursor semantics `GET
  /chat/stream/{thread_id}` has used all along, through one shared tail helper.

  The diagnosis that looks obvious is wrong twice over, which is why this is not a transport
  change. `felix.side_events` is an in-process queue drained inside the agent's own loop, so
  for a durable run both ends are already in the worker; and the events do not exist to be
  bridged anyway, because the fiber calls `agent.invoke` — `_run(..., emit_events=False)` —
  which drops the deltas and the `on_tool_start`/`on_tool_end` pairs at the source. What the
  fiber *does* produce is the session log: `_append_produced` writes each assistant turn and
  its tool results as they land, outside every `emit_events` guard. So there was already a
  durable, ordered, cross-replica record of the run's progress, and one endpoint that knows
  how to tail it. No new bus, no second delivery path, no second source of truth.

  **A `session_event` frame now carries `tool_calls`, `tool_call_id` and `id`,** which it never
  has. Found while building the above, and it is the difference between a transcript and a
  transcript you can read: a client folds these rows with the same function it folds a
  `snapshot` with, reading `tool_calls` off an assistant message to open a card per call and
  matching `tool_call_id` on the tool message to attach the result. Carrying neither, an
  assistant turn that called a tool folded to an empty message and the result was dropped
  outright — so a **warm reattach** to `GET /chat/stream/{thread_id}` (one that replays events
  rather than opening on a snapshot) has always rendered a transcript with no tool calls in it.
  The snapshot has carried all three since it was written; only the incremental frame was
  thinner, which is why it read as a reattach quirk rather than a missing field. This is a
  widening — a client that ignores the new fields is unaffected — and `id` is spelled the way
  the snapshot spells it, so a turn keeps one identity whichever way the client received it.

  **Only completed messages, never token deltas.** Chunks are never persisted, so a durable
  run yields tool cards and whole assistant messages and nothing finer. That is the right
  trade for the mode whose point is that nobody is watching, and it matches what clients
  already render there.

  Two details worth knowing. The cursor is captured **before** the run is enqueued, not when
  `run_accepted` is emitted: a fiber can be claimed and start appending the moment its row
  lands, and a later read would open the stream past the progress it exists to report. And a
  run started without a `thread_id` is not a run without a thread — the fiber mints
  `{tenant}:fiber:{id}` and writes its transcript there, so those runs report progress too;
  `fiber_thread_id` is now named rather than interpolated at each end.

  `POST /chat` (202 + `GET /chat/runs/{token}`) is unchanged and stays coarse: it is a status
  read, not a stream, so it still reports only status and the final answer. A client that
  wants the transcript there reattaches to `GET /chat/stream/{thread_id}`.

- **The second run of any eval failed on Postgres, and the canary monitor stopped scoring after
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

- **`deploy/GOVERNANCE.md` told operators that `spec.policies` and `execution.mode: durable`
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

- **Every hook that judged a file by its repo-relative name was reading the wrong tree.**
  They derived that name by stripping `CLAUDE_PROJECT_DIR` off an absolute path — which is
  correct in the main checkout and wrong in a git worktree, where the file lives at
  `<project>/.claude/worktrees/<name>/<rel>`. The strip left the worktree prefix attached and
  every anchored pattern stopped matching.

  `protect-files.sh` therefore failed **open**: inside a worktree, `.env`, `uv.lock`,
  `secrets/`, generated directories and already-published Alembic revisions were all freely
  editable, silently, because `.claude/worktrees/x/.env` does not match the pattern `.env`.
  Verified by running the hook, not by reading it.

  `quality-ratchet.sh` lost every file's history the same way: `git show HEAD:<rel>` found
  nothing, so `previous` was `None`, which both bypasses the "did this edit make it worse"
  guard and prints "new file". A ratchet that exists to stay quiet about pre-existing size
  became one that reports absolute size on every edit — observed as "module is 696 lines (new
  file)" for a module months old, after a twelve-line change.

  `doc-drift-stop.sh` inspected `CLAUDE_PROJECT_DIR` directly and so reported *another*
  session's changes as this one's, blocking the turn twice in one session over files that
  session had never opened. It now reads `cwd` from the hook payload, a documented field on
  every event including `Stop`.

  Two shared helpers in `lib/command.sh` carry the rule — `hook_repo_root` and
  `hook_repo_rel`, deriving the answer from the file's own repository rather than from the
  project root — and `tests/unit/test_file_guard_hooks.py` covers the Write/Edit guards in
  both trees, which nothing did before.

- **An image sent inline reached OpenAI and 400'd on Anthropic.** A `data:` URL is how OpenAI's
  own API documents attaching an image, so it is what every SDK emits — and the Anthropic wire put
  it in a `url` source, which that API rejects. Both wires had an image encoder and only one of
  them worked, on the provider this harness defaults to. Inline images now go to Anthropic as a
  `base64` source, labelled with the media type out of the data URL rather than the `image/png`
  the parser fills in for anything unlabelled; a remote `https://` image still goes as a URL, which
  is the form that exists so the provider fetches it itself. The older `attachments` shape carried
  the same URLs and the same bug, and now takes the same path.

  A percent-encoded data URL — `data:image/svg+xml,<svg …>`, which is a legal and ordinary way to
  write an image inline — is re-encoded as base64 rather than dropped: neither provider accepts the
  percent-encoded form, so it was a hard 400 on one route and a confidently wrong answer on the
  other, decided by nothing but which model the manifest routed to. Images render on a user turn
  only, which is the sole place either API accepts one; the OpenAI wire used to send one on an
  assistant turn, which is reachable by replaying a vision thread. Both wires now render one
  normalised part list, so a message's two shapes — `content_blocks` on the turn that parsed it,
  `attachments` on every turn replayed out of the session log — cannot drift apart again. A content
  part of a type Felix does not recognise is still dropped, but is now logged rather than vanishing.

- **Adding a field to the manifest schema no longer breaks every pinned thread and every
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

  **Upgrading — the one operator action for this release.** This rotates every content hash
  exactly once. Other entries in this release also add fields to `spec`, which would each have
  rotated it on their own; folded in together with this fix, the cost is paid a single time,
  and from the next release on a defaulted field addition costs nothing at all.

  Drain in-flight durable fibers across the deploy, and expect pinned threads to need
  re-pinning. Two details the obvious reading misses:

  - **Threads that were never pinned are affected too.** Every thread gets a hash
    soft-recorded on first touch with `pin_compile=False`, and the check enforces when *either*
    side asks for it — so a thread whose manifest later turns pinning on refuses once, with
    nothing having changed.
  - **A drifted pinned thread stays refused, not refused once.** There is deliberately no
    tenant-facing pin reset. The recovery is `POST /fork`, which starts a thread with no pin
    and keeps the history; durable fibers are marked `failed` with the drift text and are not
    retried, so there is no storm — they are re-enqueued, or drained before the deploy.

- **A tool named `felix_structured_output` had its call silently swallowed.** The Anthropic wire
  reserves that name for structured output and folds a call to it back into the turn's reply — but
  the guard against a manifest binding the same name ran only when a schema was requested, while
  the fold ran on every turn. A manifest binding it through `spec.client_tools` and declaring no
  `output_schema` therefore had that tool never execute, its model-authored arguments returned as
  the final answer, and the stop reason forced to `end_turn`, with nothing logged. The fold now
  runs only on a turn that asked for a schema.

- **Removing a field from the manifest schema bricked every stored manifest that set it.**
  The schema is `extra=forbid`, which is what makes `spec.toolz` an error rather than a field
  that silently configures nothing — but `forbid` judges authored input, and a row in
  Postgres is not input. It was authored once, validated then, and has been sitting there
  since. So when `spec.model.region` was removed in #125, every manifest stored before that
  release stopped validating, and because the store is consulted ahead of the bundled YAML, a
  stale row also shadows the file it was derived from. Found on a deployment whose `quick` was
  stored two weeks before the removal: the **default manifest** answered every request with
  `spec.model.region: Extra inputs are not permitted`, a perfectly good `manifests/quick.yaml`
  sat there unreachable, and nothing said so until someone made a request.

  Stored manifests now load through `parse_stored_manifest`, which drops fields listed in
  `felix.manifests.compat.RETIRED` and warns, naming the manifest and the field so an
  operator knows to re-save it. Authoring is untouched — a PUT or a YAML file carrying a
  retired field still fails, because its author can fix it and should be told to — and a typo
  still fails on both paths, since the list is explicit rather than blanket tolerance. Adding
  to `RETIRED` is now the price of removing a field, and only for a removal that is inert; one
  that changes how an agent compiles still needs a migration that rewrites the rows.

- **`POST /v1/chat/completions` validated `name` and `tool_call_id` and then discarded them.**
  Only `role` and `content` were forwarded, so an OpenAI SDK doing the standard tool round-trip
  sent a result whose id was dropped and the model received a tool message answering nothing in
  particular. Both fields now reach the message the model sees.

### Security

- **A caller's `taskId` became a thread id with none of the checks a client's suffix
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

- **Log injection is now closed by the text formatter, not by remembering to wrap a value.**
  A newline in a logged value ends the record and starts one the attacker wrote in full — a
  forged line that reads as a *refusal* is the damaging case, because the log is what an
  incident gets reconstructed from. The fixes for this had all been per call site: escape
  this value, then the next one someone finds. That closes instances and never the class,
  and the list of call sites only grows.

  `FELIX_LOG_FORMAT=json` never had the problem — `json.dumps` escapes the separator because
  the message is a *value* there, not a line — so the exposure was the text format alone.
  Text now escapes the caller's message before rendering it: whatever was interpolated, the
  message is one line. The JSON format is left alone rather than double-escaped.

  **Tracebacks are covered too, by indenting each line and then escaping it.** They were the
  half a message escape does not reach: `logging.Formatter` appends `exc_text` after the
  message, and an exception's own `str` is not indented the way its frames are — it renders at
  column 0, so a newline inside an exception message produced a fully record-shaped line on any
  of the ~30 `exc_info=True` call sites whose exception text is built from a caller-influenced
  value.

  Escaping the *block* would have closed that by flattening the traceback onto one line, which
  is unreadable and the reason it was left open. Splitting first and escaping each line keeps
  the shape and closes the hole: no text is dropped, an operator still reads what the exception
  said, and only the record itself begins at column 0. Escaping as well as indenting matters
  because indentation is only a claim about columns — `\x1b[1G` is cursor-horizontal-absolute,
  so an ESC reaching a traceback redraws that line at column 0 however far right it was
  written. **Frame lines now sit two columns further right than a stock Python traceback, and a
  tab inside a frame's source line renders as `\t`** — the visible changes to existing logs.

  The escape is applied to a copy of the record, and deliberately not in a `logging.Filter`,
  which is the shorter-looking option the stdlib docs invite: one record is shared by every
  handler attached, so escaping there would corrupt a JSON handler's output in order to fix a
  text handler's bug. A newline is a separator in one format and an ordinary character in the
  other, so the escape belongs where the grammar is chosen.

  `felix.manifests.compat.one_line` is gone; `logging_setup.loggable` is the one helper for
  this job. The two were not identical — `one_line` escaped every non-printable character and
  `loggable` only the C0 range and DEL — so `loggable` picked up the stricter behaviour
  rather than the shorter one. It now escapes U+2028, U+2029 and U+0085, which end a line for
  a JavaScript-based log viewer even when `tail` shows one, and the bidi overrides, which
  reorder a record's visible text without changing a byte of it. Call sites keep using
  `loggable`: the formatter cannot truncate, so the bound on an attacker-influenced string
  still lives there.

- **A caller-supplied `response_format` reached the model unscreened.** Every string leaf of a
  JSON Schema sent to `POST /v1/chat/completions` — `title`, `description`, a property name — is
  serialised verbatim into the provider request, and because per-request options are resolved once
  and reused, it sat in front of the model on *every* turn of the loop rather than on one.
  `apply_inbound_screening` iterates messages; this arrived on `model_options`, the one place it
  does not look, so content screening and input guardrails never saw it. A schema is now screened
  on the same path as the turn it rides with, and refused rather than redacted — rewriting a
  description would silently change the contract the caller is holding.

  The size bounds were also not the ones the code claimed: node count is orthogonal to bytes, and
  900 KB of schema fits in six nodes, so one accepted request could have that re-serialised into
  the provider body on every turn against the operator's own credential — and on Anthropic, where
  the schema is a tool definition inside the cached prefix, destroy the conversation's prompt cache
  as well. There is now a byte bound. `$id` and `$dynamicRef` are held to the same local-reference
  rule as `$ref`, since `$id` is what decides where a `#` pointer resolves; a property *named*
  `$ref` is no longer mistaken for one.

- **Inbound screening reported success and changed nothing on a multimodal turn.** A message
  carrying images holds its text in `content_blocks`, both wire formats prefer those over
  `.content`, and screening wrote only `.content` — so PII redaction and the `[quarantined]`
  substitution ran, were audited as applied, and the model was still shown the caller's original
  text. Nothing in `packages/harness` read `content_blocks` at all, which is why no test could see
  it. The screened text now replaces the text blocks, with the images preserved in order; blocks
  are rebuilt only when screening actually changed something, so an ordinary turn is untouched.

- **A tenant id was validated as a thread-id prefix and used as a path segment.**
  `assert_valid_tenant_id` rejected `:` and `#` — the delimiters the `{tenant}:{suffix}`
  thread-id rule needs — plus leading and trailing whitespace, and nothing else. But a tenant
  id is also interpolated into object-store keys (`artifacts/{tenant}/…`,
  `workspace/{tenant}/…`, `skills/{tenant}/…`, `manifests/{tenant}/…`), into idempotency
  keys, and into every log record. Those grammars have their own separators, and
  `acme/../other` and `acme\nWARNING  all clear` both passed. Under a claim-mode JWT verifier
  the tenant id comes from a token claim, which on Cognito is frequently user-writable.

  Nothing was exploitable through it, and the reason is worth stating rather than implying a
  breach. Outside `development`, a claim-mode verifier with an empty `FELIX_ALLOWED_TENANTS`
  refuses to start, so the attacker-writable claim has to match an allowlist entry exactly.
  Behind that, `storage/fs.py` re-validates every key segment and re-checks containment with
  `relative_to`, S3/GCS keys are literal so `..` is a character rather than a parent, the RLS
  GUC is bound as a parameter and compared for equality, and `LogIdsFilter` already escapes
  the `tenant_id` log *field*.

  What was unescaped is a tenant id interpolated into a log *message*, which four sweeps in
  the worker did (`jobs/scheduler.py`, `jobs/anomaly.py`, `jobs/continuous_eval.py`); those
  now use `loggable()` like the rest of the repo.

  The tenant id is now held to the same rule `storage/fs.py` applies to a key segment, using
  the same expression: letters, digits, `.`, `_`, `-`, and never `.` or `..` alone. One
  definition of a safe segment rather than two that drift. **A tenant id containing any other
  character is now rejected at authentication** — the shapes issuers actually emit (slugs,
  UUIDs, domains, `org_`-style ids) are unaffected; an email address used as a tenant id is
  the one shape that stops working.

- **One thread could forge another's client-tool waiter key.** A waiter name is a *key* —
  whoever can construct it can answer the wait behind it — and
  `f"client:{thread_id}:{tool_call_id}"` was not injective, because both parts may contain the
  separator. `thread_id` carries colons legitimately (`{tenant}:{suffix}`, and
  `{tenant}:fiber:{id}` for a durable run), and `tool_call_id` arrives off the model wire with
  no charset check at all (`wire/openai_completions.py` takes `str(tc.get("id") or "")`).

  Concretely, and reachable rather than theoretical:

  ```
  thread acme:fiber:F123, call call_9        ->  client:acme:fiber:F123:call_9
  thread acme:fiber,      call F123:call_9   ->  client:acme:fiber:F123:call_9
  ```

  `fiber` is a legal thread suffix — `effective_thread_id` rejects only `:` and `#` — so the
  second thread is one any caller in that tenant can create. Posting a `tool_result` for it
  resolved the durable run's pending client tool with content the poster chose.

  **Scope, stated precisely**, because the general shape is narrower than "any two-part `:` join
  is exploitable". An ordinary thread is `{tenant}:{suffix}` with `:` rejected in the suffix, so
  it carries exactly one colon and the old join was already injective for it. Only a thread
  namespace the harness mints with an *extra* colon collides — `{tenant}:fiber:{id}`,
  `{tenant}:a2a:{task_id}` and `{tenant}:eval:{run}:{item}`. Same tenant only: a tenant id
  carrying the delimiter is refused at issuance, so the prefix cannot be forged. And exploiting
  it needs the victim's fiber id (a `uuid4`) and its pending `tool_call_id`, neither of which is
  disclosed to a third party — so this is a latent hole rather than a trivially drivable one.

  Note which part was *not* the problem: `tool_call_id` is the unvalidated input, and it is not
  what made the collision reachable. The second grammar here was one the harness produced itself.

  Waiter names are now composed by `waiters.waiter_name`, which percent-encodes each part (`%`
  before `:`, so the escape cannot itself be forged) before joining. The approval and UI prompt
  waiters go through it too — their ids are a `uuid4().hex` and a `token_urlsafe`, so they were
  never ambiguous and their names are **byte-identical** to before; routing them through one
  helper is so the next part added to a waiter name is escaped by construction rather than by
  whoever remembers.

  **Upgrade note.** Client-tool waiter names change shape, so a client-tool call already in
  flight across a rolling upgrade will not be answered by the new process and times out after
  `DEFAULT_TIMEOUT_SECONDS` (120s), returning `[error/timeout]` to the model. That is the
  fail-closed direction and it resolves itself on the next call; approvals and UI prompts are
  unaffected because their names did not change.

### Changed

- **Traces can be sent to a backend Felix does not host.** Every `FELIX_OTEL_*` setting is
  now passed through `deploy/docker/compose.yml`, so `make up` plus a few lines of `.env`
  exports to any OTLP destination — a collector, a hosted vendor, a console self-hosted
  elsewhere. `x-felix-env` carried no OTLP key and there is no `env_file`, so the only way
  to get a span out of the Compose stack was to run an overlay that stood a backend up
  *inside* the project. That is how `compose.memoturn.yml` came to run one vendor's API,
  worker and console on Felix's own Postgres, Valkey and MinIO: 8 services, a database
  bootstrap, a blob bucket and a reverse proxy for a product Felix only sends to. That
  overlay and `make up-memoturn` are **removed**; point `FELIX_OTEL_ENDPOINT` at the
  instance instead, and run the product from its own compose project or its cloud.

- **`make down` takes the whole project down.** Every overlay shares the project name
  `felix`, so services one overlay started are orphans to the next; `down` named only the
  base file and left them running. A stack accrued four overlays' worth of containers,
  several receiving nothing because the last `up` had recreated `api` and `worker` without
  their env. `down` now passes `--remove-orphans`, and `make down-all` adds `--volumes`.

- **`felix doctor` reports whether the OTLP exporter is installed**, in every environment
  including `development` — which is what Compose defaults to, so a row placed with the
  production posture checks would have been skipped for exactly the operator it is for.
  `FELIX_OTEL_ENABLED=true` on a lean image logs one warning at startup and exports nothing
  while every other signal says the deployment is healthy. The check asks the question the
  exporter asks — `felix.observability.tracing.exporter_available` selects the module by
  `FELIX_OTEL_PROTOCOL`, so an environment carrying only the http exporter under `grpc` is
  reported as broken rather than fine.

- **`make up-observability` pins the whole transport.** Now that the base stack passes every
  `FELIX_OTEL_*` setting through, an operator whose `.env` held the documented hosted config
  — `FELIX_OTEL_PROTOCOL=http`, `FELIX_OTEL_INSECURE=false`, a credential in
  `FELIX_OTEL_HEADERS` — would have carried all three into the overlay: a TLS handshake
  against the collector's plaintext port, or an HTTP POST to its gRPC one, and a vendor
  header sent to a collector that never asked for it. Every span dropped, silently, with the
  stack looking healthy. The overlay owns the destination, so it now owns the protocol, the
  TLS flag and the headers too. `migrate` and `scheduler` drop the credential as well —
  neither calls `setup_observability`, so it was a secret in `docker inspect` for nothing.

- **`make up-observability` no longer forwards `FELIX_OTEL_HEADERS`.** Now that the base
  stack passes that variable through, an operator with a hosted-ingest credential in `.env`
  who then ran the overlay would have sent that `Authorization` header to a local collector
  that never asked for one. The overlay redirects the destination; the credential does not
  follow it. `migrate` and `scheduler` pin export off and drop the header for the same
  reason — neither calls `setup_observability`, so both would have been carrying a secret
  they cannot use.

### Fixed

- **One broken recall channel silently returned no memories at all.** `recall()` runs three
  channels in one transaction, and the comment above their error handler promised a deployment
  mid-upgrade would "lose a channel, not the turn". It did not: the first failure aborted the
  transaction, so every later channel died on `InFailedSqlTransaction` and the turn got nothing
  — logged at DEBUG. Reproduced by dropping a generated column, and confirmed to have been
  position-dependent, which is why it could sit there: breaking the *last* channel looked fine.
  Each statement now runs in its own savepoint, verified across all eight combinations of
  broken channels. The failure log line is now `recall channel vector unavailable` rather than
  `recall vector channel unavailable`; nothing in the tree matched the old string.

- **`recall(kinds=[...])` could return nothing while a matching memory was stored.** The filter
  ran in the ranking pass, after each channel had been cut to its budget — so a match outside
  that window was filtered against an answer it had already been excluded from. Reachable by an
  agent through the recall tool's `kind` argument and by an operator through
  `GET /memory/recall?kind=`. The predicate is in all six channels now.

- **Which memories an agent was given could differ between two identical recalls.** `recall()`
  runs three channels on each backend and fuses them by reciprocal rank. Every channel sorted
  on its score alone — a small integer for the text channels, so ties are the normal case —
  and each is then cut to a per-channel budget before fusion. Reciprocal-rank fusion scores on
  *position*, so a different candidate set entering it is amplified rather than absorbed. The
  ranking pass then tied again on score and recency. All six channels and the ranking now end
  on the row id.

  The recency tiebreak also read `last_used_at or created_at`, and `last_used_at` has no writer
  anywhere: the migration adds the column, `put_memory` sets it to `None`, the upsert excludes
  it. So "newest breaking ties" named a key that never applied. The dead half is gone, and
  restoring it changes no test, which is what says it was dead.

- **The facts an agent remembers could differ between two identical requests.** `list_active`
  sorted by writer trust, then importance, then recency, and truncated to a limit — with no
  key below those three, and all three tie routinely: facts are written in a batch so
  `created_at` collides, trust is one of a handful of values, importance usually defaults. So
  which fact fell off the end was whichever the backend happened to return, and these are the
  facts injected into a compiled prompt. Both sorts (prioritised and not) and `as_of` now end
  in the row id, which is the second half of the primary key. Which facts tie is still
  arbitrary — there is no finer recency signal to recover — but it is the same arbitrary
  answer on both backends and between calls.

- **A job's "most recent runs" could be its oldest.** `list_runs` ordered by `started_at`
  alone, and `started_at` is milliseconds — a sweep records a burst of runs inside one. With
  no tiebreak, the most recent two of five was whichever two the backend happened to return,
  and on the in-memory twin Python's stable sort made it the two *oldest*: an operator opening
  a job's history to see why it failed was shown its first attempts. Both arms order by
  `(started_at, run_id)` now, which is total because `run_id` is the last part of the primary
  key. Which runs tie for recency is still arbitrary — there is no finer signal than
  `started_at` to recover — but it is now the same arbitrary answer on both.

- **`GET /jobs` had no order.** The twin returned dict insertion order and Postgres whatever
  the plan produced, so an operator's inventory could differ between two consecutive calls and
  the twin could not stand in for the store. Both order by name.

- **The audit and usage listings silently dropped rows.** Their cursor carried only a
  timestamp, and `ts` is milliseconds — so paging asked for `ts < last_seen` and stepped over
  every other event sharing that millisecond. Those events were returned by no page at all.
  A single turn writes a user event, a tool call and a final response microseconds apart, so
  this is the ordinary case, not an edge one: five events in one millisecond read two at a
  time returned two, with a well-formed 200 each time. An audit trail that quietly loses rows
  is worse than one that is missing, because it is still believed.

  Both stores now order by `(ts, id)` — `id` is the second half of the primary key, so the
  order is total — and page on that pair (`felix/cursors.py`). A cursor issued by the previous
  version still decodes, to the position it used to mean, because one may be in a client's
  hands. Found by a new conformance contract, and pinned over the wire as well as in the
  stores, since the cursor is a query parameter a client round-trips.

- **A null byte in a message stopped the process writing audit rows.** JSON permits `\u0000`
  in a string and Postgres text does not, and a turn's audit payload carries the user's own
  message — so `{"content": "a\u0000b"}` was a request any authenticated client could make
  that halted the compliance record for the life of the API process. The insert raises, the
  batch is requeued at the front so nothing is dropped, and the flush loop retries the same
  poisoned batch every interval until the buffer's ceiling starts discarding the oldest
  events, with only a repeating warning to show for it. `record_event` strips it now. The
  in-memory twin stores anything, so only the Postgres arm of the new contract can see this.

- **A malformed cursor was a 500.** `/audit` and `/usage` passed a client-supplied string
  straight to `int()`, so `?cursor=abc` was a server error for what is a bad request — and a
  page for whoever watches the error rate. Both return 400 now.

- **The manifest twin accepted a canary weight the database refuses.** `0001_baseline` carries
  `CHECK (canary_weight BETWEEN 0 AND 100)`, and `set_canary`'s in-memory branch validated
  nothing — so a weight of 150 stored happily on `memory://` and raised an `IntegrityError` on
  Postgres. The value is not inert: it feeds the canary hash router, so that weight diverted
  every request to the canary on one backend and was unreachable on the other. The REST route
  already bounds the field; the store now does too, because plugins and worker jobs call it
  directly.

- **The manifest twin handed back the stored document by reference.** `get_version` returned
  the dict it holds, so a caller that edited what it was given silently rewrote what every
  later reader of that version saw. Postgres deserialises fresh JSONB per read and never had
  the problem — a corruption with no write in sight, on the backend the whole suite runs
  against. It returns a copy now.

- **The worker's per-tenant sweeps read nothing under row-level security.** Every HTTP request
  is wrapped in `async_run_with_context`, which binds `rls_tenant(...)`, so the fifty-odd
  tenant-scoped store functions inherit `app.tenant_id` and none of them binds explicitly. The
  worker has no request context and nothing supplied one, so `run_due_jobs_all_tenants`,
  `run_anomaly_scan_all_tenants` and `run_continuous_eval_all_tenants` ran with the policy
  unable to match any tenant. It filters rather than errors, so each sweep read an empty table
  and reported success: scheduled jobs never fired, the anomaly scan found nothing, and no
  canary was ever benchmarked — silently, and only on deployments where RLS is the isolation
  mechanism. The bundled compose role is a superuser and skips the policy entirely, which is
  why local development and CI never showed it. Each sweep now binds the tenant it is sweeping.

- **`create_fiber` and `get_fiber` bind their tenant too.** They take one as an argument and
  were the only writes in `durability/fibers.py` that neither bound nor bypassed. On the HTTP
  path the ambient context covered them; the fiber scheduler reaches `get_fiber` without one.
  Bound rather than bypassed, so the policy still enforces and a create cannot land under the
  wrong tenant.

- **The two backends disagreed about which grant authorises when several match.** Postgres
  ordered `decided_at DESC LIMIT 1`; the twin scanned a dict and returned the first row it met,
  which is the *oldest*. With two live grants for one call signature they handed back different
  rows — and with them a different `principal_subj` binding and a different `edited_args`, so a
  tool would run with the arguments an operator had substituted on one backend and without them
  on the other. Both arms now order by `(decided_at, created_at, id)` descending, so ties
  resolve identically too.

- **An expired approval hid a live one, and only on Postgres.** `find_approved` took the newest
  approved row with `LIMIT 1` and checked expiry *afterwards*, so one lapsed grant could hide a
  still-valid older grant and the call was denied. The in-memory twin scanned every row and
  skipped expired ones, so it authorised the same call. `create_pending` reuses only *pending*
  rows, so approved grants accumulate per signature — an operator re-approving after a short
  TTL lapsed produced exactly that pair, and got a working tool on `memory://` and a refusal on
  the system of record. Expiry is now part of the `WHERE` clause.

- **A tenant holding a batch of Temporal-backed fibers starved its own ordinary ones, on
  Postgres only.** `_claim_due_postgres` applied `LIMIT FIBER_BATCH` in SQL and dropped
  Temporal rows afterwards in Python, so 50 or more of them filled the batch with rows that
  were then discarded and the claim returned nothing at all — that tenant's real fibers never
  ran. The in-memory twin skips Temporal rows while scanning and never counts them toward the
  batch, so it had no such problem and nothing compared the two. The filter is now part of the
  `WHERE` clause. Measured on a live database: starvation begins at exactly 50 Temporal rows,
  and the fix also halves the query time at 200k rows by discarding rows before the sort rather
  than after, removing a 16 MB on-disk spill.

- **A re-claimed fiber stayed at the front of the queue on the in-memory twin.** The claim
  orders by `updated_at`, and the Postgres path advances it while the twin did not — so on the
  twin one fiber could be re-picked ahead of everything else indefinitely, where the system of
  record shares the scheduler out round-robin. The twin now advances `updated_at` and `version`
  on claim, as Postgres does. (The test for this was itself wrong first time and CI caught it:
  a batch claim stamps every row it takes with the same instant, so two fibers claimed together
  tie and Postgres resolves the tie arbitrarily. It asserts the advance on one row now.)

- **A manifest declaring `keep_recent_tokens: 0` silently ran with 20000.**
  `POST /chat/compact` built its strategy with `int(getattr(spec, field, default) or default)`,
  which treats a declared `0` as absent — and the schema allows `0` (`ge=0`) for both
  `reserve_tokens` and `keep_recent_tokens`. So compaction kept 20000 tokens of recent context
  whatever the manifest asked for, found nothing older to summarise, and answered `ok` having
  called no model at all. The declared window was not the one the route used and nothing said
  so, which is this repo's signature defect shape. Found while trying to write a test for the
  summarising branch and being unable to make it fire.

- **The thinking level was written twice and only one copy was read by the run.** The snapshot
  resolves it from a `thinking_level_change` event; the next turn resolves it from thread
  metadata and turns it into a thinking budget on the model spec. Nothing covered the second
  path, so a thread could display "high" and run with thinking off. Now pinned on the spec the
  provider is built from, which is the only place the difference is visible.

- **The management stores leaked between tests.** `_memory_datasets`, `_memory_items`,
  `_memory_runs`, `_memory_jobs` and `_memory_approvals` are process globals that nothing
  cleared. Writing an eval dataset named `smoke` in one test changed the item count another
  test asserted against the bundled `smoke` fixture, and the failure surfaced as an off-by-one
  in a file that had not changed. Each store now exports its own
  `reset_*_for_tests()` beside the globals it clears, following the convention
  `reset_documents_for_tests` and `reset_search_index_for_tests` already set, and the autouse
  fixture calls those rather than reaching across the package for six private dicts. The
  session-state reset added last cycle now uses the `reset_thread_meta_for_tests()` that
  already existed and had no caller.

- **A conformance contract for the jobs store** (`tests/conformance/test_jobs_store.py`), run
  against the in-memory twin and Postgres. Its Postgres half ran only under
  `test_migrations.py`, which creates the schema and never queries it. The contract covers the
  semantics the scheduler depends on: that re-publishing a job keeps its run history rather
  than resetting it, that deleting a job takes its runs with it so a name reused later does not
  inherit a stranger's history, and that truncating a run list keeps the newest.

- **A conformance contract for the audit store** (`tests/conformance/test_audit_store.py`),
  run against the in-memory twin and Postgres. Audit's Postgres half ran only under
  `test_migrations.py`, which creates the schema and never queries it, so everything asserted
  about the compliance record was asserted about a list of dicts. The contract covers what a
  dict scan and a `SELECT` are easy to differ on and what the `/audit` route depends on:
  ordering, filters composing with a cursor, and paging a history exactly once.

- **`felix.audit.store.clear_memory()`**, and audit and usage added to the suite-wide reset in
  `tests/conftest.py`. Audit was the one management store with no way to reset it, and both
  have two process globals apiece — a buffer and an in-memory twin — so an event recorded
  without a flush waited for whatever flushed next. Two worker cron tests found this the hard
  way: they passed alone and failed in the suite.

- **`felix mint-jwt` printed a token you could not use.** It went through rich, which wraps to
  the console width, and a 2048-bit RS256 token is around 550 characters — so
  `TOKEN=$(felix mint-jwt --sub ops …)`, the invocation `deploy/GOVERNANCE.md` documents,
  captured seven lines of base64 with newlines through the middle. The command exited 0, the
  token looked right on screen, and every request made with it was rejected as an invalid
  token. It is printed plainly now, and a test mints one and verifies it through the same
  `verify_jwt` the API uses.

  Two more of the same shape in the same command: `--tenant` accepted a value the verifier
  always refuses, minting a plausible token that answered `tenant_not_allowed` on every
  request; and with no signing key configured it died on an unhandled `RuntimeError` naming
  neither the setting nor what to put in it. Both now exit 2 with a message.

- **`felix eval` printed its run record through the same renderer.** Any value longer than the
  console width was split mid-token with a newline after the key, so the record of a real run
  — where model answers are much longer than 80 characters — was corrupt. CI parses this
  output, and survived only because the mock fixtures are short.

  The record is now JSON on one line, and the progress line moved to stderr, so stdout is the
  record and nothing else: `felix eval … | jq .` works. `scripts/eval-counter-smoke.sh` parses
  it instead of matching substrings against a Python repr — including a negative match for
  `error` scanned across the whole blob, which any score row quoting that word would have
  tripped. This changes the shape of `felix eval`'s stdout; anything reading it as a Python
  repr needs updating.

- **`felix temporal-worker` named its database connections `felix-cli`.** The CLI's root
  callback stamps the process role, `stamp_process_role` is first-write-wins, and this
  subcommand runs a worker for as long as the process lives — so a durable-execution worker
  appeared in `pg_stat_activity` as indistinguishable from somebody's shell, which is the one
  thing that stamp exists to prevent. The root callback now leaves long-running subcommands to
  name themselves, matching the `felix-temporal-worker` console script.

- **`felix migrate` met the in-memory database URL with a stack trace.** `memory://` is what
  `.env` ships for tests, so arriving at `migrate` with it set is ordinary, and the result was
  `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:memory` under a rich traceback
  that named neither the setting nor a value to use. The refusal lives in `migrations/env.py`,
  the one funnel every Alembic entry point passes through — `alembic current`, which
  `docs/UPGRADING.md` tells operators to run, hit the same traceback — with a friendly exit 2
  on the CLI path on top of it.

- **`felix bundle-manifests` stdout was not machine-readable.** The summary line shared the
  stream with the JSON, so `felix bundle-manifests | jq .` failed. The summary goes to stderr
  now. Its JSON went through rich as well; unlike the token, that one never corrupted anything
  — today's bundle is short enough to survive rendering — so it is printed plainly as a
  precaution rather than a fix.

- **Every `felix` subcommand is invoked by a test** (`tests/unit/test_cli_commands.py`).
  `tests/unit/test_entrypoint_wiring.py` proved each `[project.scripts]` target resolves to a
  callable, which is where the console script ends; nothing ran the bodies, and `version`,
  `migrate`, `mint-jwt`, `bundle-manifests` and `temporal-worker` had no test at all. Every fix
  above is what running them found. Each test asserts the contract the command has with
  whatever consumes it — a shell capturing a token, a parser reading the bundle, Postgres
  reading a connection name, an operator reading an error — rather than the exit code alone.

- **The eval gate can now fail, and the coverage floor now applies locally.** Two CI gates were
  passing without testing anything. `fixtures/eval/smoke.json` gives every item a `mock_answer`
  that satisfies its own rubric, so the mock eval run passed by construction: a scorer rewritten
  to `return True, 1.0, "x"` left the step green, and so would one that never ran. There is now a
  counter-smoke, `fixtures/eval/negative.json`, whose every item violates its own rubric and whose
  run must exit non-zero, plus `tests/unit/test_eval_gate_can_fail.py` asserting the same pair
  locally, per rule and in both directions, including through the CLI — the exit code is the only
  part of the gate CI reads, and the three lines that produce it had no test at all. The counter-
  smoke also asserts *why* each item failed, since `start_run` counts a raised item as a failure
  too, so a scorer that crashed on everything read exactly like one that rejected everything.
  The set of rules the counter-smoke must exercise is read off `_score_answer` by AST rather
  than written down, so adding a scoring rule fails until a fixture item has seen it reject
  something — otherwise a new rule lands with the gate silently partial.

  One malformed item no longer abandons the whole eval run. `start_run` converted each item's
  rubric *outside* the per-item `try`, so a rubric that was not a mapping raised past the
  handler and took every other item's score with it — the run reported nothing rather than
  reporting one error and scoring the rest. It is that item's error now, which is also what
  makes the counter-smoke's fourth check reachable at all.

  The counter-smoke's four checks live in `scripts/eval-counter-smoke.sh`, which the CI job and
  `make check-ci` both call. They started as two copies of the same shell and drifted within a
  day: the local one accepted a bare exit 1, so an item that *errored* instead of being scored
  down passed before a push and failed in CI. `start_run` counts a raised item as a failure, so
  the counts alone cannot tell a scorer that rejects everything from one that crashes on
  everything — which is why the scorer no longer raises on an unparseable `min_chars` either.

  Proved by mutation, twenty-two of them, each red: an always-passing scorer, a deleted CLI
  exit-code mapping, a deleted coverage floor, `check` pointed back at the coverage-free target,
  a CI step no longer running it, an empty `contains` passing again, a negative `min_chars`
  accepted, `contains` reordered above `expect`, the answer generator drifting back to
  truthiness, the smoke rubrics flattened, the floor moved back into pyproject, the CI step
  pointed at the wrong fixture, the CLI printing a summary instead of the run dict, a fixture
  item edited to satisfy its rubric, one removed, a new scoring rule with no item to cover it,
  the same rule delegated to a helper so a filtering scanner would miss it, the `min_chars`
  guard reverted, and the shared script losing its exit-code and its errored-row check.

  The scorer also stopped disagreeing with the answer generator it scores. `_score_answer` read
  its rubric keys with `or` while `_mock_answer` reads the same keys with `is not None`, so
  `{"expect": ""}` — an item whose right answer is the empty string — was scored against the
  non-empty rule its author never wrote. And an empty `contains`, one unfilled field away in any
  hand-authored dataset, matched every answer: a rubric that could never say no, passing silently
  in the direction that hides problems. It now fails closed as `invalid_rubric`.

  The coverage floor moved off the `.github/workflows/ci.yml` command line onto one `make test-cov`
  recipe that `make check` and CI both run. Before this, `make check` measured no coverage at all,
  so the floor existed only inside CI and a local run could not tell you what CI would say. It is
  deliberately not `fail_under` in `[tool.coverage.report]`, which arms on every run that measures
  coverage while `[tool.coverage.run]` still names all five roots — so adding `--cov` to a one-file
  run exits 1 at 17% with every test passing, and a guard that fires when nothing is wrong teaches
  people `--no-cov`. Ratcheted to 79 against a measured
  80.96% with extras and 80.36% lean. An invariant now fails if the floor disappears, ratchets
  down, or stops being what `make check` and CI run.

- **The worker's periodic tasks are executed by tests, and their schedules are pinned**
  (`tests/unit/test_worker_cron_tasks.py`). Six of the eight had never been run by anything:
  `test_worker_instrumentation.py` asserts each is *wrapped* and `test_worker_tenant_sweeps.py`
  runs two, so the rest were covered only by importing. That is worse than an ordinary coverage
  gap, because the worker is the only thing that runs periodic work — audit and usage flush,
  retention, memory consolidation, the job scheduler and the fiber resume live here and nowhere
  else, and a body that stops working takes its whole responsibility with it silently, since
  nothing downstream complains about work that never happened.

  Each body now runs for effect: the flushes drain their buffer into the store, a due job fires
  and a disabled one does not, retention prunes what is past its TTL and leaves what is not, and
  the fiber scheduler advances a due fiber. The eight cron strings are read off the source by
  AST and compared against a written-out table, so a schedule changed from `*/1` to `0 3` — a
  one-character edit turning a minute into a day — fails instead of shipping. Each proved by
  mutation.

- **A conformance arm where the tenant policy is actually enforced**
  (`tests/conformance/test_rls_enforcement.py`). Every other contract in that directory connects
  as the database owner, which is a superuser in CI and in the bundled compose image — and a
  superuser bypasses row-level security, FORCE included. So the policy was unreachable from the
  whole suite, and everything it protects was asserted only by reading the SQL: deleting
  `rls_bypass()` from a cross-tenant sweep left every test green.

  That blind spot had already cost something. The worker's per-tenant sweeps bound no tenant and
  therefore read nothing under an enforcing policy, and no test could see it. This arm connects
  as a `NOSUPERUSER NOBYPASSRLS` role with the listener told `database_rls=True` — the shape of a
  managed-Postgres application role — and pins the states that differ: an unbound write is
  refused, an unbound read is silently empty (which is why the worker bug survived), a bound
  tenant cannot reach another tenant's rows, and the bypass that maintenance sweeps declare is
  what lets them cross.

  Verified against a cluster built from the CI service images: 8 of 8 pass, the full conformance
  suite is unaffected, and no role, schema or policy leaks. Proved by mutation in both
  directions — removing `rls_bypass()` from `list_tenants_with_events` fails exactly the sweep
  test, and granting the role `BYPASSRLS` fails the guard that exists to catch it.

- **A conformance contract for the manifest store** (`tests/conformance/test_manifest_store.py`).
  The active pointer is what every request resolves through and the canary beside it decides
  what fraction of traffic gets a different agent, and until now the Postgres half of both ran
  only under `test_migrations.py` — which creates the schema and never queries it. Eighteen
  tests over versioning, the active pointer, the canary and the tenant boundary; both defects
  above were found by writing it, and both are proved by mutation.

- **A regression guard for the binding** (`tests/unit/test_worker_sweeps_bind_the_tenant.py`).
  It observes the context variable at the moment each sweep calls into its per-tenant worker,
  which needs no database — the failure this catches is the absence of a binding, not anything
  Postgres does with it. It also pins that the binding is unwound between tenants: a leaked one
  would be worse than none, since the next tenant's queries would run under the previous
  tenant's policy.

- **A conformance contract for the fiber claim path** (`tests/conformance/test_fiber_claim.py`).
  `test_fiber_store.py` already covered attempts through backoff and burial; this covers the
  step before it — which fibers a scheduler tick picks up and what claiming does to the row.
  That is where the two implementations are least alike: Postgres selects with
  `ORDER BY updated_at ... LIMIT ... FOR UPDATE SKIP LOCKED` and filters in Python, the twin
  scans a dict and filters as it goes. Both defects above were found by writing it. Covers due
  and terminal states (parametrized over `FIBER_TERMINAL_STATUSES` rather than one arbitrary
  string), the lease and its expiry, Temporal rows and the batch bound, oldest-first ordering,
  and that the sweep is deliberately cross-tenant.

- **Conformance contracts for the approvals store and for session search.** Both seams had a
  `memory://` twin whose Postgres counterpart ran only under `test_migrations.py`, which creates
  the schema and never queries it — so everything asserted about them was asserted about the
  twin, while `tests/unit/test_invariants.py` requires only that a twin *exists*. Both defects
  above were found by writing these contracts, not by reading either implementation.

  The approvals contract covers the semantics that are security properties rather than storage
  details: which grant authorises when several match, expiry, `bind_principal`, `one_shot` and
  its consume-once check-and-set, and the tenant boundary. The search contract covers what both
  engines can be held to — an appended event becoming findable, deletion removing it, the tenant
  and thread boundaries, and masking surviving into the index. Ranking, stemming and the hit
  shape are named as *not* covered rather than left for a reader to assume: the Postgres arm
  returns a `rank` key the twin never produces, which is visible through
  `GET /chat/sessions/search`.

  A generic `store_settings` fixture replaces the per-seam pattern, so adding a seam is now a
  contract file rather than another fixture.

- **The management routers are tested over the wire, with real scopes.** `routes/jobs.py` and
  `routes/eval.py` received zero requests anywhere in the suite and `routes/audit.py` had one;
  between them they are the operator's whole view of what the harness scheduled, evaluated and
  refused. Nineteen tests in `tests/e2e/test_mgmt_routes.py` cover jobs CRUD and its runs list,
  eval datasets and runs, the audit log and its metrics rollup, and approvals through to a
  decide that records who made it. They run under `auth_mode=api_key` rather than the suite's
  usual `none`, because `require_mgmt_scopes` is skipped entirely when auth is off: a scoped
  route tested without auth proves the handler works and says nothing about who may reach it.
  Each positive case uses a key holding only the scope under test rather than `admin`, which
  satisfies every gate by design — so a deleted `require_mgmt_scopes` call fails the test
  instead of passing it. Proved by mutation.

- **The run controls are tested over the wire.** Sixteen tests in
  `tests/e2e/test_chat_run_control.py` cover steer, follow-up, abort, continue, fork, rewind,
  compact, thinking and the UI prompt bridge — endpoints where "returned 200" is least like
  "did the thing", and which until now had no test at all. Each reads the state back: abort
  sets both the rendered `phase` and the flag the loop actually checks, a fork copies the log
  and records its parent and does not move when the branch is written to, a rewind moves the
  leaf and a 404 moves nothing, compaction of a short thread calls no model while a real one
  summarises and is billed, and answering a pending UI prompt releases the waiter that was
  blocking on it. Each is proved by mutation.

- **The e2e spy records the prompts and the model specs.** `ProviderSpy.prompts` /
  `texts_seen()` and `ProviderSpy.specs` make assertable a class of behaviour the reply cannot
  show: the reply is scripted, so a run that ignored a steer, skipped compaction or ran with
  the wrong thinking budget answers identically.

- **Thread state is reset between tests.** `_memory_session_stores`, `_meta_by_thread` and
  `_leaf_by_thread` are process globals that nothing cleared, so a test reusing another's
  thread id inherited its transcript, leaf and phase. The suite was correct only because every
  id in it happened to be unique.

### Added

- **Agents can search the documents an operator ingested.** `spec.document_tools` binds a
  retrieval tool per ref, and `support` uses it as `search_docs` over whatever this deployment
  has in its corpus. The corpus landed a while ago — ingestion, a hybrid store, both backends,
  `/documents` management routes — and nothing agent-facing could read it, so an operator could
  fill it and no agent could use it.

  It is the mildest of the three retrieval tools by construction: `http_fetch` lets the model
  choose a destination and `web_search` lets it choose a query against an operator-chosen
  endpoint, while this reaches only rows already in this deployment's store, in the calling
  tenant. So there is no address to validate and no egress to guard. The tenant comes from the
  compile rather than the call, which is the one thing the tool adds over the store it wraps.
  Its transport is `documents`, absent from the trusted allowlist, so the same content
  screening covers a retrieved chunk as covers a fetched page — and every line of a chunk is
  indented under its hit, so a `2.` at column zero can only have come from the renderer. A
  chunk is the one field here that is both untrusted and legitimately multi-line, so the
  flattening `web_search` uses on a title is not available; without the indentation one
  document renders as two, with a source the agent is told to follow.

  Retrieval is hybrid for the agent as well as for the operator: the tool builds the same
  embedder `/documents/search` builds per request, so a deployment with `FELIX_MEMORY_EMBEDDER`
  set does not answer the operator's query and quietly miss the agent's.

  This closes the audit finding that opened the capability workstream: `support.yaml` declared
  `tools: [calculator, list_skills]` — a support agent that could not look anything up.
  `fetch_docs` gave it a page whose URL it already knew; this gives it the question an operator
  actually asks, which is where something is written down.

- **A conformance contract for `recall()`** (`tests/conformance/test_memory_recall.py`), the
  first this path has had. It deliberately does not assert the two backends return the same
  hits: the twin scores text by raw token overlap while Postgres stems, so the same query can
  legitimately match different rows. What it pins is that the answer is *decided* — that a tie
  inside a channel, or between two candidates fused from different channels, resolves the same
  way every time and on either backend.

### Known and deliberately unfixed

- **An eval dataset item written with unrecognised keys is stored empty.** `items` is
  `list[dict[str, Any]]` and `put_dataset` reads only `user_input` and `rubric`, so an item
  spelled any other way — including the `input`/`expect` the bundled JSON fixtures use — is
  accepted with 200, listed as present, and stored with an empty prompt and an empty rubric.
  The dataset then looks configured and scores nothing.
  `test_an_eval_item_with_unrecognised_keys_is_stored_empty` pins it. Rejecting unknown keys is
  an API decision rather than a bug fix, so it is raised in `docs/ROADMAP.md` next to the
  eval-scoring-depth item rather than changed here.

- **A steer queued on an idle thread is dropped without reaching anyone.**
  `POST /chat/steer` with the default `kind: steer` answers 200 with `{"queued": "steer"}` and
  the snapshot then reports one queued item; the next turn clears the count and the text
  reaches neither the model nor the transcript. From the client's side that is
  indistinguishable from delivery: accepted, counted, gone. `kind: follow_up` is the idle path
  and is delivered. A steer is meant to interrupt a run already in flight, so having nothing to
  interrupt is arguably the caller's mistake — but nothing tells them.
  `test_a_steer_queued_while_idle_is_dropped_without_reaching_anyone` pins it so that making it
  error, redirect, or hold the message is a deliberate act with a failing test to rewrite.
  What it should do is a product decision, tracked in `docs/ROADMAP.md`.

## [0.2.2] — 2026-08-25

### Fixed

- **The Helm chart no longer pins the previous release's image.** `Chart.yaml`'s
  `version` and `appVersion` and `values.yaml`'s `image.tag` track the release,
  but `RELEASING.md` listed only the nine Python version fields and both greps it
  offered matched Python files alone — so `v0.2.1` shipped a chart still pinned to
  `0.2.0`. `helm install` from that tree deployed the image `v0.2.1` existed to
  replace: the one where a migrated database returns no rows to a deployment that
  has not opted into RLS. Anyone who installed `v0.2.1` by chart got `0.2.0`;
  reinstall or `--set image.tag=0.2.2`. The procedure now counts twelve places and
  greps for all of them.


## [0.2.1] — 2026-08-25

### Fixed

- **A migrated database no longer returns nothing to a deployment that has not
  opted into RLS.** `0006_tenant_rls` applies `ENABLE` *and* `FORCE ROW LEVEL
  SECURITY` unconditionally, while its header described the migration as
  optional — "enable with `FELIX_DATABASE_RLS=true`" — which is the runtime half.
  With that flag false (the default) the `after_begin` listener set no GUC at
  all, so the policy's `tenant_id = current_setting('app.tenant_id', true)`
  evaluated to `NULL`, and every one of the 16 tenant tables returned zero rows.
  Silently: no error, just empty results. Only a superuser or `BYPASSRLS` role
  escaped it — which is what the bundled compose stack uses, and why this never
  appeared in local development while being a total outage on managed Postgres,
  where you are not superuser. The listener now declares `app.rls_bypass`
  explicitly when RLS is off, which is what `database_rls=false` means; tenant
  scoping in the query layer is unchanged and remains the primary isolation.
  `FELIX_DATABASE_RLS` is now a genuine runtime toggle — flip it and restart, no
  migration needed. The migration stays unconditional on purpose: one that
  produced a different schema depending on the environment it ran in would not be
  reproducible, and there would be no way to enable RLS later without re-running
  DDL.

- **An RLS transaction that cannot name its tenant says so.** With
  `FELIX_DATABASE_RLS=true`, a transaction whose tenant did not resolve also set
  no GUC and saw nothing. Filtering is the correct answer there — a bypass would
  be a hole — but it was indistinguishable from an empty table. It now logs at
  WARNING naming `rls_bypass()` and `rls_tenant()`.

- **`felix doctor` reports RLS coherence.** The schema half and the runtime half
  can disagree in either direction and neither shows up in a request: policies
  without the flag means the app bypasses them, the flag without policies means
  nothing enforces it, and policies plus the flag plus a superuser connection
  means the policies are skipped anyway.

- **Streamed `parallel` and `plan_execute` runs are metered.** `_yield_model_stream`
  drove the model through `model.stream()`, which yields text and nothing else, and
  never called `record_usage` — the sole feed for `limit_state.tokens_input`,
  `tokens_output` and `cost_usd`. Their synthesis and planning inferences were
  therefore invisible to `limits.max_input_tokens`, `max_output_tokens` and
  `max_cost_usd`, and produced no usage row, metric, or plugin sink record, while the
  non-streaming twins of the same methods metered correctly. A declared spend ceiling
  that only holds when you do not stream is not a ceiling. `reflect`'s verifier call
  was unmetered on both paths and is now recorded too. No bundled manifest uses these
  patterns, so this reached manifests that declare `spec.pattern: parallel` or
  `plan_execute`.

- **`reflect` no longer passes an answer it could not score.** `_score` returned
  `0.8 if len(answer) > 40 else 0.4` on any exception — above the 0.7 default
  `ReflectSpec.threshold` — so an unreachable verifier, a rate-limited one, or a reply
  of `"Score: 0.9"` that `float()` rejects all silently *passed* the gate that exists
  to catch bad answers, with nothing logged. It degrades to the same
  `_heuristic_judge_score` fallback `_judge_score` uses, says so at WARNING, and reads
  the first number out of a reply rather than assuming a bare one.

- **`ABSOLUTE_LIMITS` is indexed, not `.get()`.** A missing key resolved to `None`,
  which means "no cap at all" — the posture `effective_limits` exists to prevent. Its
  values are also coerced per field, since the dict mixes `int` and `float` and the
  `int` budgets were being filled from a `float`.

- **The built-in command-screening deny rules are type-checked.**
  `_DEFAULT_COMMAND_RULES` typed its decision as `str`, so they were never checked
  against the `Literal` that `CommandRule.decision` requires.

### Changed

- **`stream_turn` is declared on the `ModelProvider` Protocol.** It was reached by
  `getattr` and left off the published contract, so a third-party provider could
  implement that contract in full and still land in the unmetered `stream()` path with
  nothing to tell its author why.

- **Each composite pattern is implemented once.** `_DelegatingAgent` carried an `_x`
  and a `_stream_x` per pattern; the copies drifted, which is what produced the
  metering defect above. They now share one `_run_*(input, *, emit_events)`, the shape
  `patterns/react.py:_run` already uses. The agent moved to `patterns/delegating.py`
  and the deep pattern's plan tools to `patterns/plan_tools.py`, taking
  `patterns/__init__.py` from 920 lines to 152 and removing both of its `noqa: E402`
  imports.

- **The HTTP model client is one class per wire format.** `_OpenAIClient` and
  `_AnthropicClient` replace a 593-line class that branched on a
  `style: Literal["openai", "anthropic"]` flag in three places — the seam the
  `ModelProvider` Protocol and the provider registry above it already described.

- **The governance wrappers take their schema types instead of `Any`.**
  `apply_command_screening`, `apply_content_screening`, `apply_limits`,
  `apply_guardrails`, `apply_judges` and `wrap_final_response_judges` read typed
  attributes rather than `getattr(config, "field", default)`, which wrote every default
  twice and made a renamed field fail *open* — `getattr(screening, "enabled", False)`
  disables screening silently. `_EffectiveLimits` is now `EffectiveLimits`.

- **A test that needs an optional extra fails in CI instead of vanishing from it.**
  `tests/unit/test_temporal_backend.py` gated six tests on `temporalio` while the CI
  test job installed `--dev` only, so they never ran — and a module-level
  `importorskip` collapses to one collect-time skip, so they never appeared in the skip
  count either. Tests now gate through `require_optional(module, extra)`, which skips
  locally and fails under `FELIX_REQUIRE_OPTIONAL_EXTRAS=1`; CI sets it and installs
  the extras that gate tests. The coverage floor moves 60 to 70, matching the measured
  number.

- **`make check-ci` and `make conformance`.** Six gates CI runs had no make target, so
  `make check` could pass while CI failed.

## [0.2.0] — 2026-08-24

### Added

- **`ApprovalRule.when_args` gates a rule on the arguments a call carries.** Approval
  rules matched on tool *name*, which is the wrong granularity when a tool is harmless
  in one shape and a privileged operation in another. `remember` is ordinary capture
  until it carries a `topic_key`, at which point it retires whatever else holds that
  key — the same outcome `forget` is gated for, reached without touching `forget`, and
  `recall` prints every stored key so they are enumerable. Gating the whole tool would
  put an approval in front of every memory write. Names are not validated against the
  gated tool's schema, so a typo yields a rule that never fires and still passes
  `validate-manifest` and the attestation checks; a bind-time warning is the fix and is
  recorded in the roadmap.

- **`POST /chat/stream` honours `spec.execution.mode: durable`.** `POST /chat` enqueued
  a fiber and returned 202 with a `resume_token`; the streaming route did not mention
  the field at all, so a manifest asking for durable execution got it on one route and
  was silently ignored on the other. It now streams the run's progress: the first frame
  carries the token, status changes are reported as they happen, and a completed run
  emits its final message. A disconnect tears down the *poll* rather than the run,
  which is what durable is for — the opposite of the transient path, where a hung-up
  client deliberately kills the run so it stops burning tokens.

- **`FelixClient.prompt` waits for a durable run.** It returned the 202 receipt as
  though it were the answer, so a caller switching a manifest to durable got
  `{"status": "accepted", …}` where the content used to be — no error, just the wrong
  shape. It now polls to a terminal status and emits progress. `wait_s` bounds the
  wait: `0` returns the receipt without polling, and exhausting a budget returns
  `status: "waiting"` rather than a failure, because the run is still going and the
  token still resolves.

- **`GET /chat/history` can be paged, and is bounded.** It returned every message a
  thread had ever had, so the response grew for the life of a thread with no way to ask
  for less. `limit` takes the *newest* events — `get_events(limit=n)` takes the first
  n, which for a transcript is the wrong end — and `before_seq` pages backwards from
  the `oldest_seq` a response hands back. The default is unchanged; lowering it is a
  breaking change for a shipped client and belongs to a product decision.

- **Store conformance covers sequence allocation.** `append_batch` returns the sequence
  numbers it allocated, asserted against both backends, because the in-memory twin
  counts a list where Postgres reads and locks — a value that is right for one arm
  proves nothing about the other.

### Changed

- **All four HTTP middleware layers are pure ASGI.** Starlette implements
  `BaseHTTPMiddleware` with a task group, an `anyio.Event` and a zero-buffer memory
  object stream per request, and every response chunk crossed four of them. `/health`
  went from 651.6 µs to 125.3 µs and an SSE chunk from 77.6 µs to 1.5 µs. An invariant
  now bans `BaseHTTPMiddleware` at the source level so the tax cannot be reintroduced
  by an `@app.middleware("http")` decorator.

- **The database pool and worker count are configuration.** `pool_size=5,
  max_overflow=10` was written literally into two engine constructors, so fifteen
  connections per worker was a ceiling nobody could raise without editing the source.
  Now `FELIX_DB_POOL_SIZE` (10), `FELIX_DB_MAX_OVERFLOW` (20),
  `FELIX_DB_POOL_TIMEOUT_SECONDS`, `FELIX_DB_POOL_PRE_PING`, and `FELIX_WORKERS` —
  the last of which was a bare `os.environ` read, invisible to `felix doctor` and
  absent from `.env.example`.

- **The resume stream backs off while a thread is quiet.** A fixed 1 Hz poll per client
  until 300 seconds of silence is 100 queries/second across a hundred reattached tabs.
  The poll now decays toward `FELIX_STREAM_RESUME_POLL_MAX_SECONDS` (10) — but only
  after thirty seconds of silence, because backing off costs first-event latency and
  the moment a user is most likely to act is right after they reattach.

- **Recall surfaces every kind of memory, as reference material.** Recall filtered on
  `kind="fact"`, so `instruction` and `task` rows were stored and never seen. All kinds
  now reach the prompt in one `<known_facts>` block, explicitly reference material:
  nothing recalled is an instruction to follow. An earlier version of this change gave
  user-stated rules their own honoured block; that was withdrawn when the provenance
  behind it did not survive review.

### Fixed

- **The streaming body-size cap silently did nothing.** `body_limit_middleware` wrapped
  the request and handed it to `call_next`, which ignores its `request` argument
  entirely — so the capped receive channel was never read, and a chunked upload with no
  `Content-Length` had no limit at all. That is the case the wrapper was written for.

- **`FELIX_DURABILITY=temporal` had never worked.** `@workflow.run` rejects a class
  declared inside a function — the worker re-imports it by name inside its sandbox —
  and the definitions were built inside `_defs()`, so every call raised. The two entry
  points failed differently, which is why nobody noticed: `start_fiber_workflow` failed
  into its caller's `except Exception`, logged a warning and let the Postgres scheduler
  run the chat, while `felix temporal-worker` failed outright. A failed start now
  records `backend: fibers` and `backend_fallback: temporal_start_failed` on the row,
  because degrading quietly is defensible and degrading invisibly is what let this sit.

- **Retirement of a memory is a decision by whoever retired it.** Resurrection was
  gated on the row's *writer*, so an operator deleting an agent-written row — nearly
  every row, and exactly the population the memory route exists to clean up — left
  something any rank-1 writer could bring back by re-storing the same text, with no
  tool call at all. Every retirement route now stamps `retired_by`, and both arms are
  asserted against one table of rules rather than against each other.

- **The rate limiter's eviction sweep stalled the worker.** It ran `max(v)` across every
  tracked key inside a request, and keys are per-IP — so the defensive component's cost
  grew with the attack it exists to absorb: 1412.7 µs at 50,000 keys, now 6.7 µs. The
  steady-state hit also went from 13.92 µs to 0.37 µs, because the old code rebuilt the
  whole timestamp list on every request.

- **The bundled skills catalog was re-read on every chat request** — `rglob` plus
  `read_text`, synchronously on the event loop, so it stalled every other request on
  the worker rather than only the one that asked. 56.6 µs to 1.4 µs, and off the loop.

- **Five independent store reads on the reattach path ran in series.** 2.66 ms to
  1.38 ms against a real Postgres, and the gap widens with network latency rather than
  narrowing. `GET /v1/models` resolved eight manifests one at a time on a cold cache.

- **Credentials were re-parsed on every authenticated request** — 15.6 µs for fifty API
  keys, and the same for JWT verifiers. Now parsed once per configuration, keyed on the
  raw settings string so a rotation invalidates it without anything having to remember.

- **Four resolver caches were unbounded, three of them keyed by tenant**, so every
  tenant that resolved a manifest left an entry for the life of the process. Now
  least-recently-used with a bound; the active-pointer cache in particular carried a
  30-second TTL and was never *removed* when it lapsed, only ignored.

- **The continue endpoint read the whole thread twice** — once for `analyze_wake`, then
  again to look at its last element.

### Security

- **A client-supplied `thread_id` could forge log entries.** It arrives as
  `Field(min_length=1)` with no charset constraint and reached the log without being
  looked up first, so one request produced two lines:

  ```
  stream cursor unavailable for t-1
  ERROR:felix_api.routes.chat:tenant acme authenticated as admin
  ```

  The second is fabricated. An attacker who can write "tenant X authenticated as admin"
  into the trail makes it argue for something that never happened, and the trail is what
  an incident is reconstructed from. Every untrusted value now goes through `loggable`,
  which escapes rather than strips — an injection attempt shows as a literal `\n`
  instead of vanishing.

- **Arbitrary exception text no longer reaches API clients.** The SSE handlers relayed
  `str(exc)` from a bare `except Exception`, so a driver error, a serializer failure or
  an assertion reached an external caller verbatim — connection strings and schema
  names included. Relaying is now opt-in per exception type through one funnel; anything
  else gets `internal error (request <id>)`, which keeps a report joinable to its
  traceback without the traceback travelling.

- **A rejected secret path was logged unescaped.** `FileSecrets.get` logs the requested
  path when it rejects it — a value chosen by whoever asked and, on that branch, one the
  loader has just refused. A newline in it forged an entry among genuine rejection
  records, and the forged one could claim an *accept*. An AST test now enforces that the
  module logs key names and never values.

### Added

- **Reconnect to a chat stream after it drops.** `GET /chat/stream/{thread_id}`
  replays what a client missed and then tails the thread; frames on both streams now
  carry an `id:` a client hands back as `Last-Event-ID`. A cold reconnect opens with a
  `snapshot` frame, a warm one replays only the events after the cursor. The cursor is
  the session log's own sequence rather than a per-connection counter — a counter
  restarts at 1 on every reconnect and so means nothing to the next connection. Only
  structural frames carry an `id:`; deltas arrive per token and stamping them would
  cost a query each, and SSE leaves `lastEventId` untouched on frames without one.

  This does **not** change what happens to the run: a disconnect still tears it down,
  deliberately, so a hung-up client stops burning tokens. What a reconnect recovers is
  the thread as it stands, not the abandoned turn. Tailing polls the shared session
  log, so it works whichever replica served the original turn and needs no Redis.

- **Memory is on in `governed` and `cowork`.** Everything above shipped inert:
  `capture` was disabled in all eight bundled manifests and `recall.tools` defaulted
  off, so no agent wrote or read a memory while the published docs described the
  feature as working. Both manifests now capture durable facts (3 per turn, 200-char
  floor) and expose the governed recall tools. Verified end to end against a real
  model and a real pgvector Postgres: the agent chose sensible topic keys
  (`ops.deploy_runbook_location`), both facts from one turn shared an ordinal, and
  recall found them through the full-text and topic channels.

### Fixed

- **`spec.memory.capture.model` was declared and never read**, so fact extraction ran
  on the turn's model — a small mechanical job billed to a frontier model on every
  turn, doubling the cost of having memory at all. Now honoured, falling back to the
  turn's model when the configured one cannot be built. Its default also moved from
  `llama-3-fast` to `claude-haiku`: the old default routes to Ollama, which a
  deployment with only an Anthropic key does not run, so capture would have failed
  every turn and said so only in a log.
- **Memories written through the `remember` tool carried no provenance.** The agent
  is compiled before the request's thread is known, so the bind-time `thread_id` was
  always empty and `origin_seq` never set — leaving half the store looking like
  genesis to an as-of query. Both are now resolved from the request context at call
  time. Found by running the feature, not by a test.

- **Memory management API** — `GET /memory`, `GET /memory/search` (the same hybrid
  ranking the agent sees, with the contributing channels reported so a surprising
  result is explainable), `GET /memory/as-of/{turn_seq}`, `POST /memory` and
  `DELETE /memory/{id}`, under new `memory:read` / `memory:write` scopes. An agent
  that remembers across sessions otherwise accumulates a store nobody can inspect —
  and when it starts answering from a fact that is stale, wrong, or was extracted
  from a hostile tool result, finding and removing that fact needed a database
  console. The time-travel surface is read-only: rewinding memory is a data-loss
  primitive on a shared table, and session rewind is deliberately non-destructive.

- **Agent-facing memory tools** — `remember` / `recall` / `forget` / `list_memories`,
  behind `spec.memory.recall.tools` (off by default). They are bound *before* the
  governance block, so recalled text passes through secret masking, policies, content
  screening, limits, guardrails, judges and approvals like any other tool output. The
  automatic fact prelude bypasses all of it, which matters because recalled text was
  extracted by a model from earlier turns and those turns can contain whatever a tool
  returned. A test asserts the wrapping behaviourally and fails if the binding ever
  moves below the wrapper block.

- **Memory recall is hybrid, and no longer just the newest rows.** Recall was
  `ORDER BY created_at` — the most recent facts, related to the question or not. It
  now runs three independent channels and fuses their *rankings* with Reciprocal Rank
  Fusion: full text over `content_tsv`, topic key over `topic_tsv` (so "what timezone"
  finds `user.timezone`, which shares no words with the stored value), and vector
  similarity over the pgvector column (so a paraphrase with no shared tokens is found
  at all). Fusing ranks rather than scores is what makes the channels combinable —
  `ts_rank_cd` and cosine distance are not on comparable scales, so any weighted sum
  of them would be meaningless.

  Semantic recall is optional and off by default: `FELIX_MEMORY_EMBEDDER=none` needs
  nothing installed, and recall simply skips its vector channel. `sentence_transformers`
  uses the existing `embeddings` extra; `openai` and `ollama` speak an
  OpenAI-compatible `/embeddings` endpoint over httpx, which is already a core
  dependency. A failing embedder costs the vector channel, never the turn.

- **Long-term memory rows carry supersession and provenance.** `topic_key` makes a
  newer value supersede an older one atomically; ids are content hashes scoped by
  manifest, so storing a fact twice collapses instead of accumulating duplicates
  recall would return twice; `status` distinguishes superseded from forgotten; and
  `origin_seq` / `thread_id` are now actually populated — the turn-versioning columns
  existed but nothing wrote or queried them, because `capture_from_turn` had no way
  to pass an ordinal. The session log's own `seq` is the turn clock, so there is no
  second counter to keep in step. Adds `as_of()`, which reconstructs what was
  believed at a past turn including facts since superseded, plus `forget` and
  `get_many`. Migration `0009_memory_recall`; recall itself is still to come.

- **`GET /ready` and `GET /live`.** `/health` returned a static `{"status":"ok"}` while
  the Helm chart wired **both** the readiness and the liveness probe to it — so a pod
  with a dead database reported Ready and received traffic, and a genuine dependency
  outage never restarted anything. `/ready` probes the database, cache, and object store
  (bounded, so a hung dependency fails rather than hangs) and returns 503 when any is
  down; `/live` does no I/O, because a dependency blip must not restart an otherwise
  healthy process. `/health` stays as a liveness alias so existing deploys and smoke
  tests keep working. Helm probes now point at the right one each, with a higher
  `failureThreshold` on liveness than readiness.
- **Request correlation.** Every response carries `x-request-id`, honouring an inbound
  one when supplied, and the id is attached to every log record — a single chat request
  fans out across tool calls, model calls, session writes, and audit events, and nothing
  tied them together before.

### Fixed

- **`memory_vectors` rejected every insert on Postgres.** `embedding vector(768)
  NOT NULL` has existed since `0001_baseline`, added by raw SQL and so invisible
  from `db/models.py`, and `put_memory` never supplied a vector. Every insert raised
  NotNullViolation; the only caller wraps it in `except: logger.debug(...)`, so
  long-term memory had never stored a row outside the in-memory twin and nothing
  said so. The constraint is dropped — a memory without an embedding has to be
  storable, since the design degrades to full-text when no embedder is configured.

- **Embeddings ran on the event loop, stalling every concurrent request.**
  `encode_texts` calls `SentenceTransformer.encode` synchronously, and all three
  consumers reach it while a turn is being served: tool retrieval (up to four times
  per loop step), procedural recall, and the `semantic:N` session strategy. With the
  `embeddings` extra installed, one encode blocked the whole worker — every other
  request on that process, not just the one that asked for it — and the first call for
  a model also loaded it from disk inside that stall. The work now runs in a thread.
  Tool retrieval keeps the cheap keyword path inline, since it is on the hot path and
  `tools_retrieval` is off by default; the guard that decides is pinned to the code it
  mirrors by a test that runs both. Also takes a lock around the model load, which the
  move to a thread pool is what made reachable — two concurrent misses would otherwise
  each construct the same multi-hundred-MB encoder.

- **Recalled memory facts never reached the model on a threaded chat.**
  `_assemble_messages` built the message list with the per-run prelude, then handed
  the thread to the session strategy — which builds a fresh list from the session log
  and returns *that*, discarding the prelude. So `<known_facts>` reached the model
  only on threadless invokes, which is not how anyone uses the API. The prelude is now
  applied after the render, directly following the system prompt. Position inside
  `messages` is cache-neutral (the ephemeral breakpoints sit on the tool list and the
  system block, never on a message), so the property the prelude exists to protect is
  unchanged. Every existing test checked that the block was *built* correctly; none
  checked that it survived assembly, which is how this went unnoticed.

- **One-off requests made during a turn shared the conversation's prompt cache.**
  Compaction summarising, memory extracting facts, inbound screening scoring, and branch
  summarisation each issue a model call in the middle of somebody's turn while carrying a
  completely different prefix — and each inherited the thread's cache identity. On an
  OpenAI-style endpoint `prompt_cache_key` defaults to `felix:<thread_id>`, so the side
  request churned the prefix the conversation had cached and the next real turn missed; on
  Anthropic the `cache_control` marker wrote a fresh cache entry, billed above base input,
  for a prompt never read again. `ModelChatOptions.isolate_cache` opts a request out of
  both, and every side request now sets it. Thinking is unaffected.

### Added

- **Interrupted tool calls are closed out, so a crashed run can be resumed.** A run killed
  mid-tool leaves an assistant turn holding a call with no result, and the provider rejects
  any transcript containing an unanswered tool call — so the one situation `/chat/continue`
  exists for was the one it could not handle. Each outstanding call now gets an
  `[error/interrupted]` result before the thread resumes.
- **`Tool.replay_safe` declares whether a tool may be re-run after a crash.** Whether the
  effect happened is not knowable after the fact; re-running a search costs latency, while
  re-running a payment charges twice. Defaults to `False`, so a tool that has not considered
  the question is never presented to the model as repeatable. Read-only built-ins declare
  it `True`.


- **Governance wrappers rebuilt each tool field by field**, so a field they did not know
  about was silently reset to its default on every wrapped tool — and every tool passes
  through the wrapper stack. `replay_safe` was very nearly lost this way on the same commit
  that added it. Cloning is now structural, and an invariant test asserts the round trip so
  the next field cannot be dropped quietly.

- **Store conformance suite.** `memory://` is the CI test path, and
  `tests/unit/test_invariants.py` asserted only that every Postgres-touching module *has*
  an in-memory twin — not that the twin behaves like the store it stands in for. Every
  green run was therefore evidence about the twin rather than about production.
  `tests/conformance/` runs one contract against both backends: append ordering and seq
  density, batch atomicity under concurrency, the full event round trip, query windows and
  filters, head/reset/wake, and secret masking. A new `conformance` CI job runs it against
  a real Postgres, and fails rather than skips when the database is missing, because a
  silently skipped arm looks exactly like a pass. Both backends pass the contract today;
  the point is that this is now checked rather than assumed.

### Fixed

- **Streaming runs produced no audit record for the turn.** `invoke` and `stream_events`
  were near-copies of one loop, and they had drifted: the non-streaming path emitted
  `user_input` and `final_response` audit events, the streaming path emitted neither, and
  nothing outside the pattern emitted them either. `deploy/GOVERNANCE.md` presents the
  audit log as the compliance evidence trail, and streaming is the default path for any
  chat UI, so the primary path was the one with no record. Tool-level audit was
  unaffected — it lives in the shared dispatch.
- **An aborted streaming run was not recorded to the session.** The non-streaming path
  appended the partial answer with status `aborted`; the streaming path only emitted an
  event. Both now do both.
- **A fatal tool error still drained follow-ups on the streaming path.** The
  non-streaming path returned immediately. A run that ended on a fatal tool error is not
  in a state a follow-up can continue from, so neither path drains them now, and the
  `final_response` audit records status `error`.

### Added

- **Compaction recovers from a context overflow.** The trigger is a token estimate —
  characters over four, anchored on the last reported usage — so it runs slightly behind
  the truth, and a manifest can declare a window larger than the model really has. When
  the estimate was optimistic the provider rejected the request and the run failed, even
  though the rejection was the most accurate signal yet that the conversation needed
  compacting. An overflow now forces a compaction pass and retries once; a second failure
  propagates. Throttling is deliberately excluded — it mentions tokens and limits too,
  and compacting over backpressure discards history to fix a problem a retry solves.
- **Two providers overflow without saying so, and are now caught.** One accepts the
  request and reports more input tokens than the window holds; another truncates the
  input and returns a length-stop having produced no output at all. Neither raises, so
  both were previously recorded as ordinary turns. In the streaming path recovery only
  happens before any delta has shipped, since a client that has already rendered text
  cannot un-render it.

### Changed

- **A streaming turn is one model call instead of two.** `stream_events` streamed a turn
  for display and then called `chat()` for the real answer, so every streaming turn ran
  the whole inference twice. The input was billed twice; only the second call was
  metered, so `limits.max_cost_usd` and the token budgets counted roughly half of what a
  streaming run spent and admitted about twice the intended budget; and the answer was
  sampled twice, so the text a user watched arrive could differ from the text that was
  saved. The streamed request also carried no tools, which is why the second call existed
  at all. `stream_turn` now yields display deltas and finishes by yielding the
  authoritative result — same message, tool calls, stop reason and usage. Providers that
  implement only `stream()` keep the previous two-call behaviour.
- **Streamed tool-call arguments are parsed and repaired.** Arguments arrive as JSON
  fragments concatenated across events, and models routinely emit raw control characters
  and invalid backslash escapes inside string literals. Both are repaired before parsing;
  a fragment that is still unparseable yields empty arguments — rejected by schema
  validation downstream — rather than raising and losing the turn.
- **Model metadata is one record instead of three tables.** Context window, max output,
  price, accepted request parameters, thinking support, and modalities lived in
  `patterns/capabilities.py` (longest **prefix** wins), `usage/catalog.py` (**substring**,
  and **first key in dict order** wins) and `usage/pricing.py` (substring, longest wins).
  Three rules for one question, and they disagreed: the request builder treated
  `claude-opus-4-5` as 200K while `/v1/models` published 1M for it, because `claude-opus`
  matched as a substring first. `felix/model_catalog.py` now holds one entry per family
  and one lookup; the rest are views over it. `patterns/capabilities.py` is removed —
  its `context_window` field was dead, which is how the two numbers drifted unnoticed.

### Fixed

- **Compaction ignored the model's real context window.**
  `spec.session.context_window_tokens` has a schema default of 128000 that pydantic fills
  in whether or not the operator wrote it, so a manifest on a 1M-context model compacted
  at 128K minus reserve — summarising away seven eighths of the window it was paying for,
  and spending a summarisation call to do it. An undeclared window now follows the model;
  an explicitly declared one still wins, including when it equals the default.
- **A turn cut off at `max_tokens` executed the tool calls it was still writing.** The
  ReAct loop only inspected `stop_reason` on the branch where the assistant produced no
  tool calls, so a truncated `tool_use` went straight to execution. Truncated arguments
  can still parse — `{"path": "/srv/app/tmp"}` shortened to `{"path": "/srv"}` is valid
  JSON naming a different target — and command screening judges the arguments it is
  handed, so the shortened value screened clean. Every tool call on an unfinished message
  is now failed with `[error/truncated]` and the run is recorded as `truncated`; the
  whole batch is refused, because it cannot be split into trustworthy and untrustworthy
  halves after the fact. Applies to both `/chat` and `/chat/stream`.
- **Extended thinking was write-only, so tool-using turns lost their reasoning.** Felix
  sent `thinking` on the request but discarded it from the response, and the session log
  dropped the field entirely. The provider signs each thinking block and requires it
  replayed alongside the tool call it produced, so a thinking-enabled manifest lost its
  reasoning at the first tool call and every later turn was answered without it. Thinking
  blocks are now captured, persisted on the session event, and replayed ahead of the
  `tool_use` blocks. Unsigned blocks are dropped rather than sent, since an unverifiable
  signature rejects the whole turn.
- **Long-context requests were under-priced.** Cost estimation had one flat rate per
  model, but providers that bill long context do so across the *whole* request once total
  input crosses a threshold. `max_cost_usd` is a fail-closed control reading that number,
  so a budget cap admitted more spend than it should. Price entries now accept `tiers`,
  where the highest matching threshold replaces the base rates. No bundled entry sets
  tiers — thresholds and rates move, and a stale number here mis-charges tenants — so
  they are supplied per deployment through a manifest price override.
- **A spent quota was retried like transient overload.** Both return 429, but an
  exhausted quota or a billing failure will not clear inside the request, so the full
  backoff ladder was added to a failure the caller was going to see anyway.
- **Parallel tool calls could interleave on one file.** `spec.tool_execution: parallel`
  runs a batch under `asyncio.gather` and two calls could name the same file, directly or
  through a symlink. Workspace writes now serialize on the resolved path.

- **`FELIX_LOG_LEVEL` was never applied**, and `structlog` was a hard dependency that
  nothing imported. Logging is now configured at startup, with JSON output in production
  and readable text elsewhere.
- **SSE streams had no error path, heartbeat, or anti-buffering headers.** A mid-stream
  failure truncated the body under an already-sent `200 OK` with no error event and no
  `[DONE]`, so a client could not distinguish success from failure; a long tool call
  emitted nothing and hit proxy idle timeouts; and nginx buffered the response by
  default, defeating streaming entirely. Streams now emit an `error` event and always
  terminate with `[DONE]`, send a keep-alive comment during quiet periods, set
  `x-accel-buffering: no`, and let client disconnects cancel the run instead of
  continuing to burn model tokens.

### Fixed

- **Every chat request leaked an S3 client.** `build_object_store` was called inside
  `build_tenant_agent` — once per request — and `S3ObjectStore` called `__aenter__` with
  no matching `__aexit__`, no `close()`, and no shutdown hook, so an aiobotocore client
  and its connection pool leaked each time until the process hit `EMFILE`. Object stores
  are now cached per backend configuration and closed on shutdown.
- `S3ObjectStore._get_client` had no lock, so two concurrent first-requests each created
  a client and orphaned one with no reference left to close it.
- `S3ObjectStore.get` swallowed any exception whose message merely contained `"404"` — a
  request id or a byte count would do it. It now matches the error type and the response
  status.
- **`dispose_engine()` was `cache_clear()` plus a comment plus `pass`**, so connections
  were never returned and lingered across Granian worker recycles. It now disposes every
  engine the module created.
- SQLAlchemy pools had no `pool_recycle` or `pool_timeout`, so PgBouncer / RDS Proxy /
  Cloud SQL dropped idle connections the pool still believed were live and
  `pool_pre_ping` paid a round trip to discover it. `create_engine_from_settings` also
  had no pool sizing at all, giving two differently tuned pools against one database.
- A failing object store silently stripped the system prompt: the bare `except` left
  `store = None`, so `SYSTEM.md`, `AGENTS.md`, instruction files, and object-store skills
  all vanished and the agent fell back to `f"You are {name}."`. It now logs an error.

### Security

- **Failed authentication was never rate limited.** Starlette's `add_middleware` inserts
  at index 0, so the auth middleware — registered last — ran *first*, and a 401 returned
  before the limiter was consulted. Credential guessing was completely unthrottled.
  Middleware is now registered auth → rate limit → body limit, giving the runtime order
  body limit → rate limit → auth.
- **`/metrics` was public and rate-limit exempt.** Its counters carry tenant-supplied
  manifest ids and remote MCP tool names as label values, so an anonymous scrape
  disclosed every tenant's manifest and tool names. It now requires auth and is counted.
- **Rate limiting was per-process and keyed per tenant.** `RedisRateLimiter` existed but
  was never wired, so the effective ceiling was `limit x replicas`; and under
  `auth_mode=none` every caller shared one `tenant:default` bucket, so a single client
  could 429 the whole deployment. Limits are now settings-driven (`FELIX_RATE_LIMIT`,
  `FELIX_RATE_LIMIT_WINDOW_SECONDS`), Redis-backed when one is reachable, and keyed per
  client. `FELIX_TRUSTED_CLIENT_IP_HEADER` opts into a proxy header — off by default,
  since the header is attacker-controlled unless a proxy you operate overwrites it.
- **The body limit trusted `Content-Length`.** A chunked request carries no such header,
  so its body was read unbounded. The stream is now capped as it is consumed.
- **Compute ceilings.** The `calculator` allowed `9**9**9**9` (`ast.Pow` with no operand
  bound); `search_files` compiled a model-supplied regex with no length or complexity
  bound and ran it over every file. Exponentiation is now bounded, the query is capped,
  lines are truncated before matching, patterns nesting a quantifier inside a quantified
  group are refused, and the scan runs off the event loop under a deadline.

### Fixed

- `InMemoryRateLimiter._windows` was a `defaultdict` that never evicted, so per-IP keys
  grew forever — a memory-exhaustion DoS in the component meant to prevent DoS.
- `RedisRateLimiter` did `INCR` then `EXPIRE` non-atomically; a crash between them left a
  key with no TTL, rate-limiting that principal permanently. Both now go in one pipeline.
- 429 responses now carry `Retry-After`.

### Security

- **A JWT with no `exp` was accepted forever.** Only `iss` (and `aud` when configured)
  were marked essential, and joserfc validates expiry only when the claim is present.
  `exp` is now required.
- **Shared issuers were accepted without an audience check.** A verifier configured as
  `access:example.cloudflareaccess.com` accepted tokens minted for *any* application
  under that issuer. `access` and `cognito` verifiers now require `;aud=`.
- **Remote JWKS was never fetched, and the fallback used the local signing key.**
  `_load_key_set` carried a literal "Lazy remote fetch via httpx would go here" comment,
  so the `access` and `cognito` schemes had no key source — and its fallback returned
  `FELIX_JWKS_PUBLIC` regardless of URL, meaning the local self-signing key would verify
  tokens claiming to come from Cloudflare Access or Cognito (safe only because `iss`
  still had to match). Key sets are now fetched from the issuer and cached with a 15
  minute TTL, refreshed by the API on a timer; `FELIX_JWKS_PUBLIC` is used for the
  `self` scheme only.
- **The tenant came from an unvalidated claim, and a missing claim collapsed users
  together.** `tenant_id` is the isolation boundary; in the default `claim` mode it is
  whatever the token says, and on Cognito `custom:*` attributes are frequently
  user-writable. Added `FELIX_ALLOWED_TENANTS`. A token with no tenant claim is now
  rejected rather than falling back to the issuer host's first DNS label, which silently
  placed every such user in the same tenant.
- **`require_mgmt_scopes` failed open when `app.state.settings` was absent.** Three
  chained `getattr` defaults landed on `"none"`, skipping every scope check. It now
  denies. `create_app` always sets that state, so this only fires for a sub-app or
  plugin router mounted without it.
- A malformed `;tenant=` spec silently left `tenant_mode="claim"`, quietly downgrading a
  pinned tenant to a token claim. It now logs an error.

### Fixed

- Expiry detection matched `"exp" in msg`, and `"exp"` is a substring of `"unexpected"` —
  so signature failures were reported to callers as token expiry.

### Security

- **The SSRF guard never resolved DNS.** Private, loopback, and link-local ranges were
  checked *only when the hostname was already an IP literal*, so any name resolving to
  `169.254.169.254`, `10.x`, or `127.0.0.1` passed — an attacker-controlled `A` record, a
  `*.nip.io` style name, or DNS rebinding. That is the standard cloud-metadata SSRF path,
  and it applied to MCP server URLs, A2A peers, container gateways, and model-supplied
  browser URLs. The guard now resolves the hostname and rejects the request if *any*
  returned address is blocked. Also closed: IPv4-mapped IPv6
  (`::ffff:169.254.169.254`, whose `.is_link_local` is `False`), decimal-integer hosts
  (`http://2130706433/`), carrier-grade NAT, reserved, multicast, and unspecified
  addresses, plus `.svc` / `.local` / `kubernetes.default` / `metadata.google.internal`.
  The old code also decided "is this an IP?" by string-matching its own exception
  message, so rewording an error would have started admitting private addresses.
- **Browser tools followed redirects past the check.** The URL was validated once and
  handed to `page.goto()`, but Chromium follows 3xx hops, loads subresources, and runs
  JS — and the URL is model-supplied, so a prompt-injected agent could pivot to the
  metadata service and read the body back via `op: "content"`. A Playwright request
  interceptor now re-validates every request the page makes. `path_prefix` still applies
  to the top-level navigation only, since enforcing it on subresources would break any
  real page.
- **Sandbox containers are now confined.** They ran as root with every Linux capability,
  a writable filesystem, and no PID or CPU limit — `network_disabled` and `mem_limit`
  were the only controls. Now non-root with `cap_drop: ALL`, `no-new-privileges`, a
  read-only root filesystem plus a `noexec` tmpfs, a PID limit, and a CPU quota. Images
  are allowlisted via `FELIX_SANDBOX_ALLOWED_IMAGES` (default: the built-in python image
  only), because `spec.sandboxes[].binding` is manifest-supplied and reaches `docker run`.

### Fixed

- **The sandbox timeout never worked, and a container stalled the whole API.** The
  synchronous docker SDK was called directly from a coroutine, so the `asyncio.wait_for`
  around it could never fire — nothing yields — and the event loop blocked for the
  container's lifetime, meaning a model emitting `while True: pass` froze every
  concurrent request. The call now runs on a worker thread.

- **A single 429 failed the whole run.** There was no retry anywhere in the model layer:
  `_is_provider_error` existed but was only consulted by `_FallbackClient` to advance to
  the next *model*, and no bundled manifest configures `spec.model.fallbacks`. Requests
  now retry rate limits and transient upstream failures (408/409/429/5xx) with
  exponential backoff and jitter, honouring the provider's `Retry-After` when present —
  seconds or HTTP-date. Three attempts total, so a blip is absorbed without hanging a
  run; non-retryable statuses are not retried. Falls through to `_FallbackClient`
  afterwards exactly as before.
- **Prompt caching was invalidated on every turn.** Recalled memory facts were appended
  to the system prompt, and Anthropic renders `tools → system → messages` with caching as
  a prefix match — so a block that changes whenever memory captures a fact moved the
  cached prefix constantly and `cache_read_input_tokens` would sit near zero. Facts are
  now rendered as a per-run user-role prelude, which also keeps model-extracted text —
  which can originate in tool output — out of the developer-tier instruction channel.


- **Thinking levels were broken against every current Claude model.** The Anthropic
  request builder emitted one shape for all of them:
  `thinking: {"type": "enabled", "budget_tokens": N}` plus `temperature: 1`. Both are
  **removed** on the current generation and return HTTP 400 — `budget_tokens` on Fable 5,
  Opus 5, Opus 4.8/4.7 and Sonnet 5, and sampling parameters across the whole 4.6+
  family. Request parameters are now chosen from a per-model capability table
  (`patterns/capabilities.py`): adaptive thinking plus `output_config.effort` where
  supported, the legacy budget where it is still accepted, and sampling parameters
  dropped where they are rejected. `max_tokens` is clamped to each model's real ceiling,
  and the non-streaming default rises from 4096 to 16000.
- **`stop_reason` was never read.** Both providers' responses were ignored and the value
  synthesised as `"tool_use" if tool_calls else "end_turn"`, so a reply truncated at
  `max_tokens`, a safety `refusal`, and a `pause_turn` all presented to the agent loop as
  a normal completion — a cut-off answer was indistinguishable from a finished one. The
  real reason is now read from both providers (with OpenAI's `finish_reason` translated),
  `StopReason` gains `refusal` and `pause_turn`, and the loop records such runs as
  `truncated` / `refused` with a warning and a `felix_run_stop_reason` metric.
- **Model ids, prices, and context windows were two generations stale.** Routes pointed
  at `claude-sonnet-4-5` and a date-suffixed `claude-haiku-4-5-20251001`; Haiku was
  priced at $0.80/$4.00 (actual: $1.00/$5.00), under-reporting the cost of every run; and
  `/v1/models` advertised a 200K context for models that have 1M. Routes now cover
  `claude-opus` / `claude-sonnet` / `claude-haiku` / `claude-fable`, with the previous
  logical ids retained so existing manifests keep resolving. Price lookup now takes the
  longest match, so `claude-opus-5` is no longer shadowed by a shorter key.

### Security

- **Four security controls no longer disable themselves silently.** Each degraded on a
  transient failure with `logger.debug` as the only signal — invisible at the `INFO`
  default — and no metric.
  *The LLM injection screener* returned `None` for both "clean" and "could not run", and
  both call sites read it as clean, so a missing key, an expired credential, a 429, or a
  provider outage turned `content_screening.on_flag: block` into a no-op — **including
  on the tool-output path that screens MCP, A2A, browser, and sandbox content**. It is
  now tri-state and honours `on_flag` when unavailable. An unparseable score is also
  treated as unavailable rather than clean.
  *The PII guardrail's* `_presidio_checked` was a permanent latch, so one transient
  engine-init failure pinned the process to three regexes for its entire lifetime. Only
  deterministic outcomes (package absent, no spaCy model) latch now; a transient failure
  retries. The fallback is announced at `WARNING` with a `felix_control_degraded` metric.
  *`guardrails.providers`* was unvalidated free text, so a typo (`"PII"`,
  `"pii-redaction"`) meant **no wrapper was applied at all** while `guardrails_enabled()`
  still returned `True`, so compile validation passed and nothing warned. It is now a
  closed set, like `targets` beside it.
  *Command screening* read only `args["command"]`/`["cmd"]`, so the built-in sandbox tool
  — whose arguments are `(code, path, stdin)` and which runs `["python", "-c", code]` —
  **skipped every rule while appearing wrapped**. It now inspects every
  execution-bearing argument, and every string argument for `sandbox`/`container`
  transports, where the payload is the program.

- **Untrusted content no longer reaches the system/developer trust tier.** The wrapper
  stack exists to keep tool output untrusted; three paths promoted it anyway.
  *Compaction* fed a raw transcript — including tool output from MCP servers, web pages,
  and files — to a summarizer with no fencing, then re-injected the model's reply as
  `role="system"` **and persisted it**, so it replayed on every later turn and outranked
  the user, after the original tool result had been dropped. The transcript is now fenced
  and labelled as data, the summarizer is told never to adopt instructions found inside,
  and the summary is emitted as user-role reference material.
  *The skills catalog* interpolated `skill.description` — from a tenant-writable
  `SKILL.md` in the object store — into the **system prompt** with no XML escaping, so a
  description containing `</description></skill></available_skills>` appended arbitrary
  text to the highest-trust surface. Now escaped.
  *Memory* captured model-repeated tool text as a durable fact and injected it into the
  next run's system prompt. Facts now carry provenance and render fenced as reference
  material that cannot close its own fence.
- **A safety judge with no model scored backwards.** `_heuristic_judge_score` ranked by
  keyword overlap, so for a criterion like *"must not leak credentials or secrets"*,
  output *containing* those words scored highest and **passed**, while benign output was
  blocked. `JudgeRule.model` defaults to `""`, so any manifest declaring a safety judge
  without a model got exactly that. Negative criteria now fail closed, and
  `assert_absent:` / `assert_present:` express polarity explicitly.
- **`auth.inbound.schemes` is now enforced against the caller.** It was only a
  compile-time check that *something* was set; it never constrained the request, so a
  manifest naming `[jwt]` accepted an `api_key` principal. The authenticated scheme is
  now carried on the principal (`api_key`, or the JWT verifier scheme `access` /
  `cognito` / `self`) and checked, with `jwt` acting as an umbrella for the verifier
  schemes. An empty list still allows any scheme.
- **`auth.outbound.providers` is now enforced.** It was declared and never read, so a
  manifest naming `[anthropic]` could still route to OpenAI or a local Ollama. Checked at
  compile against the resolved route for the primary model and every `model.fallbacks`
  entry.


- **`spec.limits` budgets are now enforced.** `max_wall_clock_seconds`,
  `max_input_tokens`, and `max_output_tokens` were declared in the manifest schema,
  range-bounded, and documented — and appeared nowhere else in the codebase.
  `LimitState.started_at_ms` was never set or read. Worse, `any_limit()` counted those
  fields toward `_has_boundary_control`, so a manifest satisfied the SOC 2 compile check
  *"require non-empty policies, approvals, or limits"* with `limits:
  {max_wall_clock_seconds: 600}` and got no runtime enforcement at all — the shipped
  `manifests/governed.yaml` did exactly this. A validator that attests to a control which
  does not exist is worse than no validator. All budgets are now checked before each tool
  call and at the top of each agent turn.
- Added `limits.max_cost_usd`, a per-run spend ceiling priced from the model catalog as
  tokens accumulate.
- **The default posture is bounded.** `apply_limits` was installed only when a manifest
  declared a limit, so a manifest with none had no cap on tool calls, wall clock, tokens,
  or spend. It is now always installed, and undeclared fields fall back to the documented
  `ABSOLUTE_LIMITS` (500 tool calls, 3600s, 1M/100k tokens, $1000).
- **The limits wrapper no longer fails open.** Its whole body sat inside
  `if req is not None:`, so with no request context every check was skipped. A tool
  invoked without a context is now denied rather than run unbudgeted.

### Changed

- **`spec.a2a.publish` now controls agent-card discovery, and its default changed to
  `true`.** The field was never read, so every agent was advertised regardless. Honouring
  it with the previous `false` default would have 404'd `/.well-known/agent-card.json`
  for every existing manifest, so the default now matches the behaviour deployments
  already have and the field is an opt-*out*.
- The agent card now emits `spec.skills` (it hardcoded `"skills": []`) and merges
  `spec.a2a.capabilities` alongside the transport capabilities.
- **`spec.observability.metrics` now allowlists counter names** for the manifest.
  Previously the per-manifest list did nothing; counters outside the list are dropped
  before the series is created, which also bounds Prometheus cardinality.
- **`spec.anomaly` thresholds are read from the manifest.** `jobs/anomaly.py` used
  hardcoded `MIN_VOLUME=10` / `BASELINE_FACTOR=3.0` and ignored the spec entirely, so
  `enabled: false` did not disable anything. Findings now carry the thresholds that
  produced them. `min_rate` remains unimplemented — see below.

### Fixed

- Injection quarantine and PII redaction crashed on dict-shaped tool output.
  `ToolOutput` includes `dict[str, Any]`, but both wrappers did `out.content = ...`,
  which raises `AttributeError` — so both controls silently degraded into "the tool
  crashed". Both now use the existing `_replace_content` helper, which already handled
  every shape.
- Procedural memory returned the top-k arbitrary rows when nothing matched the query
  (`return (scored or ranked)[:top_k]`), injecting irrelevant, possibly stale procedures
  as instructions on every turn.

- **Durable fibers could run the same step twice.** `resume_due_fibers` selected every
  fiber in `('running','pending')` with no lock, no limit, and no claim, while a fiber
  stayed `running` for the duration of its step. The scheduler fires every minute, so a
  step still running at the next tick was picked up and invoked again — concurrently, on
  a single node, and guaranteed with two workers. Since the `invoke` op runs a full agent
  with tools, that meant duplicated side effects and duplicated model spend. Fibers are
  now claimed with `FOR UPDATE SKIP LOCKED` plus a lease (`0008_fiber_leases`), the sweep
  is bounded, an expired lease is reclaimed so a crashed worker cannot strand a fiber,
  and `_save_fiber` uses a version compare-and-set so a lost update cannot rewind
  `cursor` and replay a completed step.
- **Concurrent appends to one thread could 500 and lose events.** `append_batch` computed
  `seq` as `max(seq)+1` against a `(tenant_id, thread_id, seq)` primary key, so two
  concurrent appends — an SSE stream plus `/chat/steer`, `/chat/tool_result`, or
  `/chat/sessions/custom`, all of which target the same thread by design — computed the
  same head and one died with an unhandled `IntegrityError`. Appends now take a
  transaction-scoped advisory lock per thread.
- **Scheduled jobs never fired for any tenant except `default`.** `run_due_jobs` takes
  `tenant_id: str = "default"` and the worker cron called it with no argument. Added
  `run_due_jobs_all_tenants`, which sweeps every tenant that has jobs and isolates
  per-tenant failures. Jobs are also claimed *before* being invoked (the minute-cron
  previously re-fired the same job on every tick until the first run completed), and the
  write-back no longer forces `enabled=True` from a stale read, which silently
  re-enabled jobs an operator had just disabled.
- The fiber sweep and the job sweep both run under `rls_bypass()`; as cross-tenant
  maintenance they previously ran with no `app.tenant_id` GUC, so enabling
  `FELIX_DATABASE_RLS` would have silently returned nothing and stalled them.

### Security

- **Upstream model-provider error bodies are no longer relayed to API clients.**
  `ModelGatewayError` embedded `body[:200]` of the raw provider response in its message,
  and both `/chat` and `/v1/chat/completions` return `str(exc)` to the caller — so
  provider request ids, organization identifiers, quota and billing detail, and any
  echoed request content reached whoever made the request. The body is now kept on
  `.body` (bounded) for server-side logging only; the client sees
  `"<provider> provider returned HTTP <status>"`. Found independently by this audit and
  by CodeQL (`py/stack-trace-exposure`).

- **Fixed an unauthenticated remote code execution path.** `spec.mcp_servers` entries
  with `transport: stdio` carry a manifest-supplied `command`, `args`, `cwd`, and `env`
  that reached `create_subprocess_exec` **at compile time**, so resolving a manifest ran
  the command — and the child inherited the API process environment (model API keys, the
  Postgres URL, cloud credentials). Writing such a manifest needed only the tenant-level
  `manifests:write` scope, and the shipped Compose defaults (`FELIX_AUTH_MODE=none` +
  `FELIX_ALLOW_INSECURE=true` + a `0.0.0.0` publish) meant it needed no credentials at
  all. stdio is now **disabled unless `FELIX_MCP_STDIO_ALLOWED_COMMANDS` names the exact
  commands allowed**; the check runs on manifest write, at compile, and at spawn. The
  child no longer inherits the parent environment — it gets `PATH`/`HOME`/`LANG`/`LC_ALL`/
  `TZ` plus the keys the ref declares — and loader variables (`LD_PRELOAD`, `PYTHONPATH`,
  `NODE_OPTIONS`, …) are rejected outright.
- **`FELIX_AUTH_MODE=none` is now refused on any non-loopback bind**, in every
  environment. `FELIX_ALLOW_INSECURE=true` relaxes the environment check only; it is no
  longer a way to serve an unauthenticated API to a network.

### Changed

- **Secure-by-default local stack (breaking).** `FELIX_HOST` defaults to `127.0.0.1`
  (containers still set `0.0.0.0` explicitly), Compose defaults to
  `FELIX_AUTH_MODE=api_key` with `FELIX_ALLOW_INSECURE=false`, and the API port publishes
  on `127.0.0.1` (override with `FELIX_BIND_ADDR`). `make up` now runs
  `scripts/dev-key.sh`, which generates a local API key into `.env` on first run and
  prints it, so the quickstart stays one command. Existing deployments that relied on
  anonymous access must set an auth mode or bind loopback.
- `felix doctor` reports the stdio allowlist and fails the loopback check when
  `auth_mode=none` is paired with a public bind.
- **`ApprovalRule.bind_principal` and `one_shot` were declared in the manifest schema
  and enforced nowhere.** `find_approved` matched only
  `(tenant, manifest, tool, call_signature, status)`, never filtered by principal, and
  never consumed the grant. So principal A's approval auto-approved principal B's
  byte-identical call in the same tenant, and a single approval authorized unlimited
  replays until it expired. Both flags are now enforced: `bind_principal` adds a
  `principal_subj` predicate, and `one_shot` adds a `consumed_at` predicate plus a
  conditional-UPDATE consume, so two concurrent identical calls cannot both spend one
  grant. Migration `0007_approval_consumed_at`.
- **Command screening's `require_approval` never created an approval.** It returned a
  deny string naming a rule, so the bundled default for `sudo` told the model to go ask
  a human who was never asked, and no operator ever saw a request. It now creates a
  pending approval, emits `approval_required`, and blocks on the decision — via the same
  path `spec.approvals` uses. `command_screening.approval_ttl_seconds` (default 300s)
  bounds the wait so a run cannot block forever on an approver who never comes.

### Fixed

- **Audit and usage events emitted while serving traffic were never persisted.**
  `emit_agent_audit` and `record_usage` are called from the agent loop, which runs in the
  **API** process, but the only `flush_pending` callers were Taskiq cron tasks in the
  **worker**. Wherever those are separate containers — Compose, Helm, the documented
  deploy paths — the worker drained an always-empty buffer while the API's grew for the
  life of the process. So `GET /audit` returned only worker-side events, metered usage
  was lost, and the API leaked memory in proportion to tool calls. The API now runs its
  own flush loop (`FELIX_AUDIT_FLUSH_SECONDS`, default 5s) and drains on shutdown.
- **A failed flush no longer discards the batch.** Both stores drained the buffer
  *before* writing, so one `commit()` failure lost those events permanently — for audit,
  that is the compliance record. Batches are now re-queued in order and retried.
- Buffers are bounded (10k events) and count what they drop, so an unreachable database
  degrades visibly instead of exhausting the process silently.
- `emit_agent_audit` logged nothing when recording failed (`except Exception: pass`); it
  now warns.

### Added

- Security scanning: CodeQL, a `pip-audit` CVE check over the locked
  dependency set (all extras), a gitleaks secret scan of the full history, and
  a Trivy scan of the image CI builds.
- Test coverage is measured and gated at the current 60%, ratcheted upward
  deliberately rather than set aspirationally.
- `tests/unit/test_invariants.py` — the repo rules are now enforced rather than
  documented: `.env.example` covers every `Settings` field, no optional
  dependency is imported at module scope, every Postgres-touching module has a
  `memory://` path, and the governance wrapper order in `builder.py` is fixed.
- `scripts/lean-import-check.py` and a CI `lean` job that imports all 156
  modules with no extras installed — the default image's promise, checked.
- `scripts/validate-toolkit.py` and a CI `toolkit` job; `.claude/**` and
  `CLAUDE.md` are now inside the CI path filter instead of bypassing every gate.
- Six settings that existed only in `config.py` are documented in
  `.env.example`: `FELIX_DATABASE_RLS`, `FELIX_SCALE_OUT`, `FELIX_REPLICA_ID`,
  `FELIX_OTEL_ENDPOINT`, `FELIX_WEBHOOK_SECRET`, and `FELIX_POLICY_BUNDLE_PUBKEY`
  (the last is declared but not yet consumed by any code path).
- GitHub Actions are pinned by commit SHA, and all container base/service
  images by digest, so a rebuild is reproducible and a retagged upstream image
  cannot change what ships.
- `scripts/test.sh` — the canonical test entry point. It sets the in-memory
  store environment the suite is designed for; `make test` and CI both use it.
- `pre-commit` now runs in CI, so the hook config cannot silently break again.
- `.editorconfig` matching the ruff configuration.
- `make type` now says what to do when the optional extras are missing instead
  of printing 27 unresolved-import errors.

### Changed

- Dependabot: weekly grouped updates for actions and images; the docker
  ecosystem now points at `deploy/docker` (the previous `/` entry matched
  nothing — there is no Dockerfile at the repo root).
- Builder image `uv` 0.9 → 0.12, `ty` 0.0.73 → 0.0.74, and the Docker build
  caches uv downloads between builds.
- Relicensed from MIT to Apache License 2.0 (adds an express patent grant and
  a trademark carve-out; contributions are inbound under the same license).
  Adds a `NOTICE` file; releases published under MIT remain MIT.

### Fixed

- The runtime image no longer ships `pip`. Its vendored copies of `msgpack`
  and `setuptools` carried HIGH CVEs (GHSA-6v7p-g79w-8964, CVE-2025-47273)
  even though neither is a Felix dependency; the venv is built by uv in the
  builder stage, so the runtime never needed pip. The runtime stage also
  applies pending OS security updates, clearing four util-linux CVEs that
  `python:3.14-slim` has not picked up yet. The image scans clean.
- `pre-commit install` failed for every contributor: the ruff repo entry was
  missing its `https://github.com/` prefix, so hook installation could never
  clone it. `pre-commit validate-config` passes on the broken file — only
  `install-hooks` surfaces it.
- `make check` failed on any machine with a `.env`: the pytest leg inherited
  `FELIX_DATABASE_URL` and ran against a real Postgres, and `make type`
  checked `tests/` while CI checks only `packages apps`.
- The Docker build no longer falls back to an unfrozen `uv sync`, which could
  silently produce an image from a different dependency resolution than CI
  tested. CI now also verifies `uv.lock` is current and installs `--frozen`.
- Taskiq worker no longer dies on idle BRPOP (`redis-py` 8 default
  `socket_timeout=5`); broker/result backend use `socket_timeout=None`.
- Scheduler entrypoint awaits `run_scheduler` via `asyncio.run` (taskiq 0.12+).
- Worker/scheduler Compose healthchecks disabled (image probe targets API `/health`).

### Changed

- Session leases prefer Redis (with in-process fallback) so exclusive/shared
  attach works across API replicas.

### Added

- Session control routes: snapshots, FTS search, abort/continue, thinking
  levels, leases, compact, UI prompts, JSONL export (see README Protocols).

## [0.1.0] — 2026-08-22

### Added

- Initial public release of **Felix** — self-hostable managed agents harness
  (`apiVersion: felix/v1`).
- Surfaces: `/chat`, OpenAI-compatible `/v1`, A2A, MCP, management APIs.
- Lean Docker Compose (api, worker, **scheduler**, Postgres+pgvector, Valkey)
  with optional MinIO (`--profile full`).
- Helm chart with PVC support, consumer shared secret, scheduler container, and
  pre-install/pre-upgrade **migrate Job**.
- Durable fibers, audit spill to DuckDB (optional), JWT/api_key auth, plugins seam.
- Durable **usage meters** (`usage_events`) flushed by the worker; `GET /usage`.
- Eval **fixture + `--mock`** path for CI (`fixtures/eval/smoke.json`).
- Chat **history** (`GET`/`DELETE /chat/history/{thread_id}`) and **audit metrics**.
- Response aliases (`events`/`plans`/`requests`/`manifests`/`datasets`) for chat-ui clients.
- CLI: `migrate`, `eval`, `mint-jwt`, `bundle-manifests`, `doctor`, `version`, `temporal-worker`.
- Typed packages (`py.typed`) for harness, CLI, API, and worker.

[0.2.2]: https://github.com/felix-run/felix/releases/tag/v0.2.2
[0.2.1]: https://github.com/felix-run/felix/releases/tag/v0.2.1
[0.2.0]: https://github.com/felix-run/felix/releases/tag/v0.2.0
[0.1.0]: https://github.com/felix-run/felix/releases/tag/v0.1.0
[0.3.0]: https://github.com/felix-run/felix/releases/tag/v0.3.0
