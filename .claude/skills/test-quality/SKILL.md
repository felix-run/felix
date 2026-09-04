---
name: test-quality
description: Judging whether tests are worth having rather than counting them — would the test fail without the change, assertion strength, using real in-memory implementations instead of mocks, brittleness, edge coverage versus line coverage, runtime and isolation, and how to ratchet a coverage floor honestly. Use when reviewing added or changed tests, when coverage rose but confidence did not, before relying on a suite to gate a refactor, or when deciding whether to delete a test.
allowed-tools: Read Grep Glob Bash(git:*) Bash(rg:*)
---

# Test quality

A test that cannot fail is worse than no test: it costs the same to run and maintain, and it buys
false confidence. Coverage percentage measures which lines executed, not whether anything was
checked. Judge the suite on what it would catch.

## The first question

**Would this test fail without the change it accompanies?** Everything else is secondary. Read what
the assertion actually pins; when it is not obvious, prove it. A test added alongside a fix that
passes on the unfixed code is the highest-value finding you can report.

In this repo, proving it means **mutation**: introduce a real violation of the thing the test
claims to pin, run the test, watch it go red, revert, and confirm the tree is clean. A probe file
under the scanned root is usually enough and leaves nothing to undo but one `unlink`.

Read the outcome carefully, because two of the three are failures of the test:

| Outcome | Meaning |
|---|---|
| **RED** | It failed on the violation. The test is evidence. |
| **GREEN** | It passed on a real violation. It pins nothing — report it. |
| **ERROR** | It errored rather than failed: an import, a missing symbol, a fixture. That is not a pass and not a failure; it says nothing about whether the test would catch the bug. Fix it until it FAILS, then re-run. |

Mutation is the only method that works for a test that *reads* the tree, which is most structural
invariants here — reverting source through `PYTHONPATH` changes what `import` resolves and nothing
on disk, so a scanning test sees the working copy whatever you do. A tool that automated the
import-driven case existed briefly and was deleted: it could not serve the tests that most needed
it, and it cost more to keep honest than doing the mutation by hand.

**Structural tests need this most.** An AST or `rglob` scan that matches nothing passes, and reads
exactly like the rule holding. One here matched `timeout=<Constant>` while every literal it hunted
lived inside `httpx.Timeout(...)`; another checked a hand-written list naming six of nine governance
wrappers. Both were green the day they were written. Assert that the corpus and the match set are
non-empty, so the day the scan stops finding anything is the day it fails rather than goes quiet.

## Assertion strength, weakest to strongest

Abbreviated — `raises` stands for the test framework's raises context manager:

```
assert result                             # passes on any truthy object — near zero information
assert result is not None                 # passes on the wrong object of the right shape
assert len(items) > 0                     # passes on the wrong items
with raises(Exception): ...               # passes on a typo raising AttributeError
assert result.status == "denied"          # pins one real behavior
assert result == Expected(...)            # pins the whole value
with raises(PolicyDenied, match="..."):   # pins the failure mode, not just failure
```

Rule of thumb: if the assertion would still pass when the function returned a *different but
correct-shaped* object, it is not testing behavior. Assert on the specific value, the specific
exception type, or the specific field — and prefer one strong assertion to five weak ones.

## Prefer real implementations to mocks

A mock tests your model of the dependency, not the dependency. Where a project ships a real in-memory
implementation of a store, queue, or clock, use it — that is a second implementation of the same
contract, and it exercises the calling code the way production does. Reach for a mock only at a true
boundary you cannot run: a paid API, a network peer, wall-clock time.

Two smells: a test that patches an internal module path (it breaks on rename and passes on a bug),
and a test whose assertions are about the mock's `call_args` rather than about any value the system
produced.

## Brittleness

These fail on refactors that changed nothing, and they train people to edit tests until green:
assertions on log text, on dict or set ordering, on wall-clock timing, on SQL string shape, on an
exception's message rather than its type, and on the full text of a generated prompt. Pin the
contract, not the rendering.

## Edge coverage over line coverage

Ask what input makes this branch wrong — empty, absent, duplicated, out of order, a second tenant, a
cancelled task, an upstream error, a value at the boundary of a limit. Read coverage for *shape*
(which branches are missing) rather than for its number.

Ratchet the floor honestly: measure the current number and set the gate to it, raising it
deliberately when it rises. An aspirational floor nobody meets only teaches people to bypass it.

## Runtime and isolation

A suite people will not run is a suite that does not gate anything. Flag the single slow test that
dominates the run, tests that share mutable state or depend on execution order, and anything reaching
a real service when an in-memory path exists. A per-test timeout is a backstop, not a budget: a test
approaching it is already a defect.

## Repo specifics

The runner and its environment, the in-memory path, the async mode, the coverage floor and where to
raise it, and how to promote a recurring finding into a permanent structural test:
[references/felix-test-map.md](references/felix-test-map.md).

## Report

Rank by carrying cost: **compounding** (a test that will mislead every future change), then
**contained**, then **cosmetic**. Each: `file:line`, what the test fails to establish, and the
stronger assertion or the missing case. List separately any test you recommend **deleting**, with the
reason it cannot fail. Say plainly when a suite is sound, and name what you did not review.
