---
name: felix-self
description: How Felix proposes and readies work on its own repository — where a ticket may come from, the evidence it must cite, the Definition of Ready it is scored against, the labels, and the three structural stops on self-audit. Use when drafting an issue, checking a ticket for readiness, or deciding whether something is worth a ticket at all.
---

> This is a summary. The source of truth is `docs/SELF.md` at the workspace root — read it with
> `read_file` before your first ticket in a session, and trust it over this file when they disagree.

# Felix on Felix: proposing and readying work

**The rule everything follows:** evidence is computed by code or observed in a real run, judged by
a person, and only *written up* by you. No evidence, no ticket.

## Where work comes from, in order

1. Real-run signals — a failed `smoke.yml` run (`github__list_workflow_runs`, then
   `github__get_job_logs`), an issue a person filed, a regression someone reported.
2. `docs/ROADMAP.md` items under **Now** — each claim **re-derived at HEAD** with `search_files`
   before it becomes a ticket. Carry the roadmap's diagnosis; re-derive its prescribed fix.
3. Self-audit — anything whose evidence is "I read the file". At most one open at a time, and it
   must name the invariant and the mutation that would prove it.

Rank 1 displaces rank 2. At most three drafts per run, at most one from the roadmap.

## Evidence shapes (one must match exactly)

An Actions run URL · `ROADMAP.md:<line>@<sha>` · a 32-hex audit event id · an eval run id ·
`usage:<manifest>:<window>` · `#<issue>` filed by a person. Anything else is `felix:meta`.

## Definition of Ready — eight points, one per section

`evidence` (a shape above) · `outcome` (one sentence a user or operator would notice; a file name
scores zero) · `surface` (kind + files expected to change) · `acceptance` (an exact command → its
expected result; "tests pass" scores zero) · `out_of_scope` ("none" must be typed) · `companions`
(`CHANGELOG.md` entry, `.env.example`+README, `make schema`, docs page, OBSERVABILITY.md) · `risk`
(`none` / `control-path` — auth, governance, screening, secrets, egress, sandbox, tenancy,
`builder.py`) · `estimate` (tool calls and turns; over 120 calls means split it).

Eight → `felix:ready`. Fewer → `felix:needs-detail` and **one** comment whose first line is
`Readiness N/8`, listing the missing sections with a fill-in you can derive (you may propose an
acceptance command; you may not invent evidence). No second comment until the issue changes.
`risk: control-path`, an ambiguous outcome, or two plausible designs → `felix:needs-human`, stop.

## Before every run

Search for an open `felix:paused` issue: if one exists, stop at once. Search for an open
`felix:meta`: if one exists, draft no self-audit. Search open issues for a duplicate before filing.

## Never

Set `p1`/`p2`/`p3`. Apply `felix:go`. File without evidence. Edit anything — you hold no write
tool here. Describe a check you did not run as done.
