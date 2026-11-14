# Code manifest and terminal client audit — 2026-11-14

Scope: the bundled workspace manifest and the companion `felix-web/apps/tui` client,
including their local tool contract, approvals, cancellation, build and tests. This is a
focused implementation audit, not a fresh security review of the whole harness or a live
production verification. Existing November 11 audit documents are preserved.

## Findings and changes

1. **The terminal could not run a build or test.** `local_shell` only emulated file verbs.
   Added explicit `--shell` native `/bin/sh` execution. The terminal always asks before a native
   command, including under `--yes`, shows its cwd and command, captures stdout/stderr, marks
   nonzero exits as errors, caps output at 200 kB and kills the process group on cancellation
   or the four-minute deadline. Commands have the user's account access; this is not a sandbox.
2. **The shell ignored `cwd`.** Both native and emulated paths now resolve the supplied cwd
   inside the workspace before execution. Escaped or non-directory cwd values fail.
3. **Unsupported emulated commands looked successful.** Unsupported commands, refusals and
   error responses now carry `error: true`, preventing a false report that a check passed.
4. **Settling a tool did not cancel its underlying work.** Workspace calls now own abort
   controllers and invalidate a pending confirmation on cancellation. Stop, thread switches,
   exit and unmount cancel active work. A later turn gets fresh controllers.
5. **`local_open` was unsupported.** Contained text files now use the terminal pager; viewer
   failures propagate to the model. Web URLs are returned for manual opening.
6. **Cowork's inherited five-minute run lifetime was too short for coding work.** The new
   `code` manifest derives from cowork but explicitly allows a two-hour resume lifetime,
   exceeding its hour-long work budget, ten-minute approvals and worker lease recovery.
   Existing caller-token expiry enforcement still applies. Cowork itself is unchanged.
7. **No coding-specific agent existed.** `manifests/code.yaml` adds repository-instruction
   discovery, preservation of uncommitted work, implementation and verification guidance,
   coding starters and a distinct greeting. It retains cowork's authenticated access,
   mutation and shell approval rules, content screening and durable execution.

## Verification

- Baseline TUI: 369 tests passed.
- Updated TUI: 386 tests passed, including real subprocess execution, stderr/nonzero status,
  cwd containment, refusal, output bounding, cancellation of a child before a delayed write,
  late confirmation invalidation, and viewer success/failure.
- TUI TypeScript, Biome and Bun build pass; built CLI help exposes `--shell`.
- Harness manifest, approval and durable-run regression tests pass: 74 tests, including
  the final `code` manifest.
- Strict bundle validation discovers and validates all 19 manifests, including `code`.
- Manifest schema is current. Web protocol parity, API drift and payload shape checks pass
  against the web repository's committed contract snapshots.

## Remaining boundaries

- The built TUI has been exercised in a pseudo-terminal against the real HTTP API, compiler,
  approval wrappers and durable fiber loop with a scripted model and temporary memory stores.
  A worker lease-expiry recovery test passes. No live vendor model or separate API/worker
  deployment with Postgres and Redis was run; a real process-crash recovery remains unverified.
- `--shell` is Unix-specific, noninteractive and unrestricted after approval. It does not
  install dependencies or provide OS isolation. Without it the existing emulation remains.
- The browser continues using its virtual filesystem and emulated shell; this change does
  not give a browser native process execution.
- Other durable agents, including cowork, still inherit the short default lifetime reported
  in the November 11 audit. This change addresses the new coding agent specifically.
- The prior audit's broader worker recovery/shutdown findings were not re-audited here.

## Use

Rebuild/restart the harness so its bundled `code` manifest is available. Build the companion
TUI with `pnpm tui:build`, then start it from the target project directory:

```sh
FELIX_ORIGIN=http://localhost:8080 FELIX_API_KEY=... \
  bun /path/to/felix-web/apps/tui/dist/felix.js --manifest code --shell
```

Keep the API key in the environment or the existing configuration file. Commands require
both the harness approval and the local terminal confirmation.

## Follow-up: shared client lifecycle and browser tools

- Reproduced four shared-client failures before fixing them: late results crossed thread
  switches; Stop did not cancel the tool port; executor exceptions escaped without posting
  an error result; and a local command exceeding the 45-second heartbeat window looked like
  a dead SSE connection because dispatch was awaiting the command instead of reading.
- The shared engine now invalidates pending results on reset/abort, cancels a supporting
  tool port, pins result delivery to the originating thread and catches executor failures.
  Its liveness check pauses while a local tool is pending, then grants a fresh read window.
- A failed client command now carries a failure marker on its local transcript card as well
  as the `error` field sent upstream. Diagnostic output before an exit error no longer makes
  a failed build look successful on that card.
- Native shell timeout/cancellation retains captured diagnostics. A real subprocess test
  verifies timeout kills a child before a delayed write; late and pre-aborted approvals
  never spawn commands.
- Browser shell unsupported/usage responses now carry `error: true`; compact echo redirection
  writes the requested file instead of returning success without writing.
- The complete shared-client suite passes 445 tests, browser workspace suite 203, and TUI
  suite 386. Type checks, lint and contract checks pass for the changed packages.
- Five scripted-model HTTP integration tests cover the actual bundled `code` agent's
  discovery, anonymous refusal, durable shell approval, denied execution, client response
  and propagation of a failed client command into the model prompt. These exercise the real
  compiler and governance wrappers without a vendor call.
- Refreshed the web snapshots from harness `cb4dde08`: paths, events, payloads and failure
  prefixes are unchanged; three snapshot provenance fields advance to that commit.

## Final broader checks

- Full Felix suite: **10,656 passed, 708 skipped**, three dependency deprecation warnings.
- Companion web chat: **1,658 passed** across 144 files. Shared client: **445 passed**;
  browser workspace: **203 passed**; TUI: **386 passed**.
- The initial web check found an incomplete local dependency installation: `shiki` was
  declared and locked but absent from the UI package's installed links. Restored dependencies
  offline with the existing frozen lockfile and installation scripts disabled. No dependency
  versions or lockfile content changed.
- The production web build then exposed existing implicit-any callback parameters in the
  Markdown component map and navigation link. Added explicit React component and router
  prop types; the final production build passes without rendering changes.
- Web first-load bundle check passes: 447.3 kB gzipped against a 450 kB budget.
- Built the updated TUI in the companion checkout. No deployment, credential change or
  live model invocation was performed.

## Browser cancellation and consecutive coding turns

- Four new regressions reproduced late browser edits after Stop/timeouts and mounted-file
  mutation after cancellation. The browser executor now aborts its underlying call at its
  deadline, checks cancellation before further mutations and aborts staged writable streams
  instead of closing them. Each engine owns its cancellation signal; navigation still leaves
  background engines running, while Stop cancels its own engine's tools.
- Calls pin the mounted-folder identity. Removing a mount or choosing another folder during
  a read refuses the rest of the call instead of redirecting an edit to the in-tab filesystem
  or the replacement folder. Cancellation preserves changes already committed; it cannot undo
  a browser filesystem operation that was already issued.
- Three shared-client regressions reproduced tool IDs incorrectly deduplicated across later
  turns, consumed IDs surviving reset and an old aborted send settling a new send. Deduplication
  is now scoped to the run, and late callbacks cannot update another run's state.
- An additional code-agent HTTP test expires a worker claim, recovers the run and verifies a
  returning stale worker cannot invoke the model twice. All six code HTTP tests pass.
- Browser workspace suite: **210 passed**; shared client: **448 passed**; web chat:
  **1,659 passed**; TUI: **386 passed**. The web bundle check passes at 447.7 kB / 450 kB.
- The full web run exposed a test timing assumption: the final text delta is visible before
  the stream's DONE marker. The rendering test now waits for actual completion before counting
  the next run's layouts; its existing render-count limit is unchanged.
- A frozen, clean install exposed OpenTUI's JSX declarations depending on React types that
  its published package does not declare for downstream resolution. Added the already locked
  `@types/react` 19.2.18 to the workspace root's development dependencies. This adds one importer
  entry to the lockfile and upgrades no package. TUI and web type checks now pass after install.

## Reusable real-stack terminal smoke

`scripts/smoke-code-tui.py` boots the real HTTP API and durable fiber loop with isolated memory
stores, registers only scripted model routes, and starts the built companion TUI in a PTY. It
answers the real harness approval and the separate terminal command confirmation, verifies a
file was not created before either decision, checks the file content and completed run over HTTP,
and verifies the shell output reached the next model prompt. The final answer is observed in
the terminal and `/quit` exits successfully. Four scripted model calls; zero vendor calls.
The first two calls now verify the connected client's native-shell capability and ordered root
and nested repository instructions before requesting the command.

Run with `uv run python scripts/smoke-code-tui.py` after building the sibling companion TUI,
or pass `--tui /path/to/felix.js`. All processes and files belong to a temporary test setup;
this does not exercise deployment storage or change an existing deployment.

## Coding workflow follow-up

The `code` manifest is now version 1.1.0 and uses the existing `deep` pattern's persisted
plan tools. Authenticated HTTP tests verify a later durable run can retrieve the earlier run's
plan and notes. Plan mode explicitly permits client capability checks, instruction discovery,
listing, reading and search, while refusing writes and commands before client dispatch.

The companion implements `local_info` in the browser and terminal. It distinguishes virtual
and mounted folders from a terminal directory, and emulated commands from opted-in native
execution. Older clients can still use the existing file tools; the manifest instructs the
agent to fall back when either new tool is unknown.

`local_instructions` is shared by both clients. It reads only `AGENTS.md` and `CLAUDE.md`
along a target file's ancestors, including for new files. Output is bounded to 16,000 characters
per file and 48,000 combined, with explicit continuation offsets and a completeness flag.
Unreadable instructions fail the call rather than appearing absent; traversal and excessive
depth are refused. Terminal tests cover an instruction symlink escaping the workspace.

Edits carry their original content to the filesystem adapter. The terminal checks it after
confirmation; mounted browser writes check before staging and before committing. Concurrent
changes return an error so the agent can reread and reconcile instead of replacing newer work.
These are optimistic checks, not filesystem locks or transactions across other programs.

Terminal delete and rename recheck the source after confirmation. Rename creates an exclusive
hard link before unlinking the source, refusing an existing destination even if it appeared
during approval. Filesystems without hard links and cross-filesystem moves are refused. A
failure after destination creation can retain both names. Browser rename retains a source whose
bytes changed during copying; browser destination creation remains a non-atomic platform operation.

Verification during this follow-up: shared client **460 passed**, browser filesystem **215
passed**, full TUI **396 passed**, and web chat **1,659 passed**. Harness coding, permission and
plan regressions passed **58 tests**. Repository-wide companion types, web and TUI builds,
protocol parity, API drift, and payload shape checks passed. The final web first-load budget
passed at **448.4 kB gzipped / 450 kB**. The real API/TUI smoke passed with zero vendor calls.
The complete harness suite was not rerun for this manifest-only runtime change. No production
deployment was performed for these follow-up changes.
