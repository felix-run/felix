# Felix builds Felix

The program by which Felix contributes to its own repository: proposes work, confirms a ticket is
complete, implements it, and opens the pull request — while a person decides what matters and merges.
This file is the spec both sides read: the `triage` and `contributor` manifests point their agents at
it, and humans use it to know what Felix will and will not do.

**The rule everything below follows:** evidence is computed by code or observed in a real run, judged
by a human, and only *written up* by the model. No evidence, no ticket. This is the structural answer
to the failure recorded in [ROADMAP.md](ROADMAP.md) under "The loop" — with nothing running real work,
the harness spent roughly forty commits auditing its own source tree.

## Program artifacts

Status updated in place as each piece lands; the ladder below is only as real as this table.

| Artifact | Status | Lands with |
|---|---|---|
| This spec, the `felix_task` issue template, the PR contract, labels | `[x]` | docs/self-program |
| Audit rows carry which control denied a call (`policy_deny.control`) | `[ ]` | feat/audit-deny-control |
| `McpServerRef.tools` allowlist, so the GitHub surface is enumerable | `[ ]` | feat/mcp-tool-allowlist |
| `manifests/triage.yaml` + `skills/felix-self` (rungs 0–1) | `[ ]` | feat/triage-manifest |
| Governed shell tool (`spec.shell_tools`, `FELIX_SHELL_ALLOWED_COMMANDS`) | `[ ]` | feat/shell-tool |
| `contributor.yaml` v2 — runs the gates, publishes over GitHub MCP | `[ ]` | feat/contributor-v2 |
| Builder container (`deploy/docker/compose.self.yml`) | `[ ]` | feat/builder-container |
| `felix-boundary` required check (`.github/workflows/felix-boundary.yml`) | `[ ]` | feat/felix-boundary |
| Eval rules that score a trajectory (`tools_called`, `max_errors`, …) | `[ ]` | feat/eval-trajectory-rules |
| `scripts/self-scoreboard.py` | `[ ]` | feat/self-scoreboard |
| Scheduled triage / implement jobs (`payload.fresh_thread`) | `[ ]` | feat/job-fresh-thread |

## The ladder

Each rung is earned by evidence from the one before. Nothing skips a rung, and there is no rung where
Felix decides which problem matters or merges anything.

| Rung | Felix does | A person does | Graduates when |
|---|---|---|---|
| **0 — Propose** | Reads real-run signals and the roadmap; drafts at most three issues per run using the `felix_task` template. Every `issue_write` pauses for approval. | Approves or denies each issue; sets `p1` / `p2` / `p3`. | ≥ 90 % of filed issues carry evidence in an accepted shape over four consecutive runs; zero duplicates of an open issue. |
| **1 — Confirm ready** | Scores every open `felix:task` against the Definition of Ready; posts one comment naming what is missing; applies `felix:ready`, `felix:needs-detail` or `felix:needs-human`. | Applies `felix:go` — the act of judgment that authorises implementation. | A person overrides Felix's verdict at most twice in ten tickets; the pinned `felix:canary` issue is always marked `needs-detail`. |
| **2 — Implement locally** | On a `felix:go` ticket: branches in the builder checkout, edits, runs `ruff`, `ty` and `./scripts/test.sh` through the shell tool, reports the literal output. | Reads the transcript. | Three tickets reach "all gates green" with real gate output in the report. |
| **3 — Open pull requests** | Publishes through `github__create_branch`, `github__push_files`, `github__create_pull_request` — each approval-gated, and the approval row's arguments *are* the diff about to leave the machine. PR body follows the contract below. | Reviews and merges. | Over ten `felix:authored` PRs: ≥ 60 % merged, median ≤ 2 review rounds, zero reverts, `felix-boundary` never tripped. |
| **4+ — not built** | Unattended issue filing; scheduled runs carrying scopes; Felix reviewing Felix; harness-side protected paths; a container gateway. | — | Designed only when rung 3 has produced its numbers. |

## Where work comes from

Ranked. A higher rank always displaces a lower one when the per-run cap binds.

| Rank | Source | What Felix reads | The draft must cite |
|---|---|---|---|
| 1 | Smoke failures | `github__list_workflow_runs` on `smoke.yml` with `status=failure`, then `github__get_job_logs` | the Actions run URL and the failing step |
| 1 | Tool errors in real runs | `GET /audit?event_type=tool_call&status=error` on the builder API, grouped by manifest × tool | an audit event id |
| 1 | Runs that ended badly | `final_response` audit rows whose status is not `ok`; `felix_run_stop_reason` when metrics are reachable | an audit event id and the reason |
| 1 | Control health | `felix_control_degraded`, `felix_control_unavailable`, `felix_rule_targets_nothing`, `felix_untrusted_tools_unscreened`, `felix_policy_unsatisfiable` | the metric name, labels and scrape time |
| 1 | Eval regressions | `GET /eval/runs` — a dataset whose `fail_count` rose after a week at zero | an eval run id |
| 1 | Cost anomalies | `GET /usage/summary` week over week per manifest, or the anomaly scan | `usage:<manifest>:<window>` with the numbers |
| 2 | Roadmap `[ ]` items under **Now** | `docs/ROADMAP.md`, and for each item the claim **re-derived at HEAD** with `search_files` | `ROADMAP.md:<line>@<sha>` plus the search and its result. The draft carries the roadmap's *diagnosis*; its prescribed fix is re-derived, never copied — the diagnosis ages well and the prescription does not |
| 3 | Issues people filed (`bug`, `enhancement`) | `github__search_issues` for open issues without a `felix:` label | the issue itself — Felix never re-files these, it only completes them (rung 1) |
| 4 | Self-audit | anything whose evidence is "I read the file" | the invariant from `.claude/rules/felix-invariants.md` **and the mutation that would prove it** |

### Accepted evidence shapes

The `evidence` field of a ticket must match one of these exactly. Anything else makes the ticket
`felix:meta`, whatever its author intended.

| Shape | Example |
|---|---|
| Audit event id — 32 hex characters | `evidence: audit 3f9c…e2a1` |
| GitHub Actions run URL | `https://github.com/felix-run/felix/actions/runs/1234567890` |
| Eval run id | `evidence: eval-run 9b1d…` |
| Roadmap line pinned to a commit | `ROADMAP.md:214@3bc66e1` |
| Usage window | `usage:contributor:2026-09-08..2026-09-14` |
| An issue a person filed | `#124` |

### The three structural stops

These are enforced by the readiness check and the scoreboard, not by asking nicely.

1. **No evidence, no ticket.** A draft whose evidence matches no shape is labelled `felix:meta`.
2. **One `felix:meta` open at a time.** The triage prompt searches for an open `felix:meta` before
   drafting; if one exists it drafts none. This is the roadmap's "one hardening / audit item per cycle"
   made mechanical. A defect found in a real run has real evidence and is exempt by construction.
3. **At most three drafts per run, at most one from the roadmap.** Rank-1 signals displace rank-2.

## Definition of Ready

The `felix_task` issue template (`.github/ISSUE_TEMPLATE/felix_task.yml`) carries the contract. A
ticket is ready when every section is present *and specific*:

| Section | Ready means |
|---|---|
| `evidence` | matches an accepted shape |
| `outcome` | one sentence a user or operator would notice; naming a file scores zero |
| `surface` | the kind of thing that changes (manifest, route, pattern, tool, worker task, docs, CI) and the files expected to change |
| `acceptance` | the exact command and its expected result — `./scripts/test.sh -k test_x` → `1 passed`; "tests pass" scores zero |
| `out_of_scope` | what an implementer must not touch; "none" must be typed |
| `companions` | which of `changelog.d/`, `.env.example` + README, `make schema`, the felix-web page, `docs/OBSERVABILITY.md` this touches |
| `risk` | `none`, or `control-path` — auth, governance, screening, secrets, egress, sandbox, tenancy, `builder.py` (the same list `.claude/hooks/pr-quality-gate.sh` uses) — which adds `felix:security-review` |
| `estimate` | tool calls and turns, inside `contributor.yaml`'s limits; over 120 calls means split it with `github__sub_issue_write` |

**The readiness check** (rung 1) scores eight points, one per section. Eight → `felix:ready`. Fewer →
`felix:needs-detail` and exactly one comment listing the missing sections with a proposed fill-in for
each Felix can derive (it can propose an acceptance command; it cannot invent evidence). No second
comment until the issue changes. `risk: control-path`, an ambiguous outcome, or two plausible designs
→ `felix:needs-human` and Felix stops.

**Priority is human-only.** `p1` / `p2` / `p3` are set by a person. Felix works only tickets carrying
`felix:go`, which a person applies, and never touches a `p*` label. GitHub cannot restrict label
writes per label, so this is *detected* rather than prevented: the scoreboard walks each issue's
timeline for `labeled` events on `p*` by the bot account and reports every one as a violation.

### Labels

| Label | Meaning | Set by |
|---|---|---|
| `felix:task` | uses the Definition of Ready template | template |
| `felix:proposed` | drafted by Felix, awaiting a person's look | Felix |
| `felix:ready` / `felix:needs-detail` / `felix:needs-human` | readiness verdict | Felix |
| `felix:meta` | self-audit; at most one open | Felix |
| `felix:go` | authorised for implementation | **person** |
| `felix:in-progress` | a contributor run has claimed it | Felix |
| `felix:authored` | a pull request Felix opened | Felix |
| `felix:security-review` | `risk: control-path`; `felix-security-reviewer` must run | Felix or person |
| `felix:paused` | on any issue: every Felix run stops at its first step | **person** |
| `felix:canary` | the pinned, deliberately vague issue the ready-check must always reject | **person** |
| `p1` / `p2` / `p3` | priority | **person only** |

## Ticket to pull request

`.github/PULL_REQUEST_TEMPLATE.md` carries the contract; for a `felix:authored` PR every section is
required and `felix-boundary` fails the check when one is missing.

- `Closes #N` — exactly one `felix:go` issue. No ticket, no pull request.
- **Evidence re-verified at HEAD `<sha>`** — the ticket's evidence re-fetched; whether it still
  reproduces. Stale evidence closes the ticket, not the PR.
- **Gates run** — each gate with the literal tail of its output. A gate not run is listed under
  **Not verified**, never described as passed.
- **Companions** — the ticket's checkboxes, ticked or explained.
- `Felix-Thread: <thread_id>` — the join key the scoreboard uses for cost and time-horizon.

Branches are `felix/<issue>-<slug>`. Pull requests open as drafts and are flipped to ready only after
`github__get_pull_request_status` reports CI green. Felix never merges, never approves, and never
requests changes on a pull request; the reviewer role is rung 4.

## The self-modification boundary

Felix may not change the things that constrain Felix. The list is enforced by the `felix-boundary`
required status check, which runs the *base branch's* definition on `pull_request_target` and reads
the changed files through the API without checking out head code — so a pull request that edits the
workflow cannot change the check that judges it, and with `enforce_admins` on, disarming it means a
person merging a change to the workflow. Until that check lands, CODEOWNERS plus required review is
the gate.

```
.github/**                                   manifests/contributor.yaml
.claude/**                                   manifests/triage.yaml
CODEOWNERS                                   docs/SELF.md
uv.lock                                      scripts/felix_boundary.py
migrations/**                                tests/unit/test_*_manifest.py
deploy/**                                    packages/harness/src/felix/manifests/builder.py
packages/harness/src/felix/governance/**     packages/harness/src/felix/tools/shell.py
packages/harness/src/felix/auth/**           packages/harness/src/felix/security/**
```

A ticket whose `surface` names one of these is `felix:needs-human` at rung 1 and a person implements it.

## Kill switches

Any one of these stops the loop; none needs code.

| Switch | Effect | Where |
|---|---|---|
| Revoke the bot's PAT | every `github__*` call fails; nothing leaves the machine | GitHub settings |
| `PUT /jobs/<name>` with `enabled: false` | scheduled runs stop firing | builder API, `jobs:write` |
| Apply `felix:paused` to any issue | every run stops at its first tool call | GitHub |
| Stop the builder container | everything stops | `docker compose … down` |

## Scoreboard

Numbers, not impressions. Computed by `scripts/self-scoreboard.py` from GitHub (read-only) and,
when `FELIX_BUILDER_URL` is set, from the builder API's `/audit` and `/usage/summary`. Printed as a
table; nothing is posted automatically in this pass.

| Metric | Source | First threshold |
|---|---|---|
| Evidence-cited issues | `felix:task` issues by the bot whose `evidence` matches a shape ÷ all issues by the bot | ≥ 90 % — **rung 0 gate** |
| Readiness at first check | rubric score in the first readiness comment, median | ≥ 6/8 for human tickets after Felix's proposal; 8/8 for Felix's own |
| Meta-work ratio | `felix:meta` opened ÷ all opened by the bot | ≤ 20 % |
| Human-priority violations | `labeled p*` timeline events by the bot | 0 |
| Verdict overrides | `felix:ready` removed or `felix:needs-detail` removed by a person within 7 days | ≤ 2 in 10 — **rung 1 gate** |
| Gates green locally | contributor transcripts whose report carries a real `./scripts/test.sh` tail with 0 failed | 3 tickets — **rung 2 gate** |
| Merged without human commits | merged `felix:authored` PRs whose every commit author is the bot ÷ merged | ≥ 50 % |
| Rework rate | `felix:authored` PRs closed unmerged ÷ opened | ≤ 30 % |
| Review rounds | review submissions before merge, median | ≤ 2 |
| Regression rate | smoke failures or eval `fail_count > 0` within 48 h of a Felix merge | 0 — **rung 3 gate**, with merge rate ≥ 60 % over 10 PRs |
| Time-horizon | tool calls and wall-clock from first to last audit row on the PR's `Felix-Thread` | tracked; a rung is revisited when the median doubles without regressions |
| Cost per merged PR | `GET /usage/summary` for the thread's manifest and window | tracked; alert at 3× the four-week median |

Detection of the failure modes the program forbids, one mechanism each:

- **Issue spam** — more than five issues by the bot in a week, or two with identical `evidence`,
  is a scoreboard violation and the next triage run is skipped.
- **Rubric gaming** — the `acceptance` command is what the implementer runs and pastes, output
  and all, under **Gates run**; the boundary check cannot run it (it checks out nothing from the
  head, on purpose), so a command that cannot run is caught at the readiness check — a ticket
  whose acceptance names no runnable command scores zero there — and a pasted result that CI
  contradicts is caught by CI.
- **Silent narrowing** — a merged PR whose ticket's acceptance command fails afterwards counts
  as rework on the scoreboard.
- **Scope creep** — changed paths outside the ticket's `surface` fail the boundary check for the bot
  and are a comment for a person.
- **Editing its own gates** — the boundary list above, plus a scoreboard grep of merged bot PRs for
  those paths (belt and braces: a control must be shown to fire).
- **Cost runaway** — a week above 3× the trailing median disables both job rows.

## Runbook — GitHub side (a person, once)

1. Create the `felix-bot` GitHub user and invite it as a collaborator with **write** on
   `felix-run/felix`. Write is needed to push branches and open pull requests; the protections
   below are what keep write from meaning merge.
2. Fine-grained PAT on this repository only. Rungs 0–1: Issues read/write, Metadata read, Contents
   read, Actions read. Rung 3 adds Contents read/write and Pull requests read/write. Store it as
   `GITHUB_MCP_TOKEN` in the builder host's `.env` and nowhere else.
3. Labels: `gh label create` each row of the table above.
4. Branch protection on `main`, before rung 3 — required approving reviews 1, require review from
   code owners, dismiss stale reviews, and restrict pushes to the maintainer. With `enforce_admins`
   already on, the bot cannot merge, cannot push to `main`, and cannot approve its own pull request.
5. A ruleset "non-felix branches": target `refs/heads/**` excluding `refs/heads/felix/**`, rule
   *restrict creations*, bypass = repository admin. The bot can only create `felix/*`.
6. Add `felix-boundary` to the required status contexts once the workflow exists.
7. Pin one deliberately vague issue labelled `felix:canary`. The ready-check must always reject it;
   the day it does not, the check has become decoration.

## Runbook — builder host

The builder is a dedicated checkout on a host that holds no cloud credentials and no Docker socket:
`make up-self` once `deploy/docker/compose.self.yml` exists. It is **never**
`~/Projects/felix` or any tree a person or another agent works in — two test runs in one tree fake a
flaky suite. `FELIX_AUTH_MODE=api_key`, because under `none` the approvals API is anonymous too and
the same caller could approve its own mutation (the `cowork.yaml` comment records why).

Trigger a rung-0 run by hand: `POST /chat` with `{"manifest": "triage", "messages": [{"role":
"user", "content": "draft tickets"}]}` and a fresh `thread_id`. Decide approvals at
`POST /approvals/{id}/decide` with a key holding `approvals:write`, or from chat.felix.run.

## What Felix never does, at any rung in this document

Sets priority. Merges. Approves or requests changes on a pull request. Edits a file on the boundary
list. Runs while `felix:paused` exists. Files an issue without evidence. Describes a gate it did not
run as passed.
