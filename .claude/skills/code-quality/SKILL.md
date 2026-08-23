---
name: code-quality
description: Judging the carrying cost of Python code — abstraction altitude and naming, complexity budgets (function length, nesting, parameters, module size), duplication that is real versus duplication that is deliberate, and type/API ergonomics — plus how to rank findings so they get acted on. Use when reviewing code for quality rather than correctness, before a refactor, when a module keeps needing edits, or when a quality hook reports that an edit made a file worse.
allowed-tools: Read Grep Glob Bash(git:*) Bash(rg:*)
---

# Code quality

Quality is what a change will cost the next person. It is not correctness (does it work) and not
style (does it match the formatter). Both of those have tools; this does not, which is why it decays
quietly until someone says a change was harder than it should have been.

## Budgets, and what each one is a proxy for

A budget is a smoke alarm, not a rule. Crossing one means look, not rewrite.

| Metric | Budget | What crossing it usually means |
|---|---|---|
| Function body lines | 60 | The function does several jobs; the extractable seam is usually a comment already in it |
| Nesting depth | 4 | Control flow that wants a guard clause, an early return, or a lookup table |
| Parameters (excl. `self`/`cls`) | 7 | A missing dataclass, config object, or a function that took on a second caller's needs |
| Module lines | 600 | The module has stopped having one subject |

Ratchet, do not gate. Judge a file against **its own previous state**, not an ideal. An absolute
threshold on a large-by-design module fires on every edit and gets muted; "this function grew from
40 lines to 80 in this change" is a finding nobody argues with.

## Altitude and naming

- One function, one level of abstraction. Mixing policy (*what should happen*) with plumbing (*how
  bytes move*) is the most common quality defect and the one that compounds fastest.
- A name should state intent, not mechanism. `process_data` and `handle` describe nothing;
  a name that needs a comment to explain it is the finding, not the comment.
- Boolean flag parameters usually mean two functions sharing a body. Look at the call sites: if every
  caller passes a literal `True` or `False`, split it.
- Comments that explain *why* are load-bearing — keep them. Comments restating the code are noise.
  Never delete a comment that records an ordering constraint or an invariant.

## Duplication that is real, and duplication that is not

Real: the same logic in two places that must change together, and nothing forces them to. Hand-rolled
parsing where a schema already exists. A wrapper re-implementing what its own framework does.

**Deliberate, and not a finding:** parallel implementations of one Protocol (a database-backed store
and its in-memory twin are two implementations, not one duplicated — collapsing them into a shared
base class couples the test path to the production path and defeats both); generated files; test
setup that reads better repeated than abstracted. Two things that merely look alike today but change
for different reasons are not duplication.

## Type and API ergonomics

- Check the Protocol against its implementations — a Protocol that only one class can satisfy is a
  class with extra steps; one that implementations satisfy only by accident is a missing test.
- Annotations on the public surface, `__all__` on modules others import, and no `Any` on a boundary
  that callers have to guess about.
- Error messages should name the value that failed, not restate the exception type. `unknown pattern
  'reakt' (known: react, plan, router)` is worth ten stack frames.
- Read the type checker's *warnings* on changed files, not just its errors. A project that downgrades
  rules to warnings for pre-existing debt still emits real defects at that level.

## What is not a finding

A lint rule the repo disables on purpose. Read the `ignore` list and its comments before flagging
anything the formatter would have caught, and never add a per-line suppression to route around one.
Repo-specific exemptions, known large-by-design modules, and the local duplication counter-rules:
[references/felix-hotspots.md](references/felix-hotspots.md).

## Report

Rank by carrying cost, because unranked quality findings get ignored wholesale:

- **compounding** — gets worse with every future change (wrong abstraction, leaking plumbing, a
  duplicated rule that must change in two places).
- **contained** — bad but isolated; costs one reader once.
- **cosmetic** — worth fixing while you are in the file, not worth a round trip.

Each finding: `file:line`, the defect in one sentence, the concrete cost — name the future change it
makes harder — and the fix. Say plainly when code is healthy, and name what you did not review.
