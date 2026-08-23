---
name: dead-code-audit
description: Proving that Python code is actually unreachable before deleting it — the reachability channels that defeat grep (import-time registries, packaging entry points, string names in config and YAML, dynamic getattr/importlib lookup, public re-exports, test-only callers) and the safe order in which to delete, verify, and confirm the exported surface did not shrink. Use when removing a function, class, module, setting, or dependency, when auditing a package for cruft, or whenever something looks unused.
allowed-tools: Read Grep Glob Bash(git:*) Bash(rg:*) Bash(python3:*)
---

# Dead code audit

Deleting live code is a far worse outcome than leaving dead code in place, and in any codebase with
dynamic dispatch a grep for the symbol name is not evidence. This is a proof procedure: clear every
reachability channel, then delete in an order that fails loudly if you were wrong.

Static tooling (vulture, ruff `F401`, coverage) is a *lead generator* here, never a verdict.

## The reachability channels

Clear all seven before calling anything dead. One uncleared channel is one production outage.

| Channel | How it reaches the code | How to check |
|---|---|---|
| Direct import | An ordinary `import` or `from … import` | `rg -w '\bNAME\b' --type py` |
| String name | Named in YAML/JSON/TOML config, a manifest, a route table, or a CLI argument | `rg -F 'name-as-written' -g '!*.py'` |
| Import-time registry | A decorator or module-level call registers it when the module is imported | Find the registry, list its keys at runtime, not by reading |
| Packaging entry point | Declared under `[project.entry-points]` and loaded by name | `rg -n 'entry.points' -g '*.toml' -g '*.py'` |
| Dynamic lookup | `getattr(mod, name)`, `importlib.import_module(f"…{x}")`, `globals()[…]` | Grep for the *prefix or module*, not the symbol |
| Public re-export | Listed in `__all__` or re-exported from a package `__init__.py` | `rg -n 'NAME' --glob '**/__init__.py'` and every `__all__` |
| Test-only caller | The only reference is a test | If so it is dead **and** its test is dead — delete both, or the test was the only spec and the code should live |

Two more that look like liveness but are not: a name referenced only in a commented-out block, and a
name referenced only in a docstring or changelog. Those are documentation debt, not callers.

## Procedure

1. **Establish the entry points.** Console scripts, ASGI/WSGI app factories, worker task modules,
   CLI commands, migrations, plugin entry points. Everything reachable starts at one of these.
2. **Run the channel table** for the symbol. Record which channel you cleared and how — a report that
   says "grepped, found nothing" has cleared exactly one of seven.
3. **Check history.** `git log -S'<symbol>' --oneline -- .` shows when it arrived and whether its
   caller was removed separately (a strong dead-code signal) or it was added for a caller that has
   not landed yet (not dead — pending).
4. **Delete, then prove.** Remove it in one commit-sized change, run the full test suite, and run an
   import sweep that imports *every* submodule rather than the top-level packages — a stray reference
   in a module nothing else pulls in stays invisible to a partial import.
5. **Confirm the exported surface.** Diff the public names before and after. If the package is
   consumed outside this repo, a removed re-export is a breaking change even when nothing in the repo
   used it.

An import sweep, dependency-free:

```bash
python3 -c "import pkgutil,importlib,sys; p=importlib.import_module(sys.argv[1]); [importlib.import_module(m.name) for m in pkgutil.walk_packages(p.__path__, p.__name__+'.')]" PACKAGE
```

## What counts as proof

- The channel table, filled in, with the command you ran for each row.
- The full suite green after deletion — not a targeted subset, because the caller you missed is by
  definition somewhere you were not looking.
- The import sweep clean.

Absence of evidence from a single grep is not evidence of absence. If a channel cannot be cleared —
a plugin loads names from an operator's config, say — the answer is "cannot prove dead", and that is
a legitimate result to report.

## Repo specifics

Which registries, entry points, string-named fields, and generated files apply here, and the exact
sweep command this repo ships:
[references/felix-reachability.md](references/felix-reachability.md).

## Report

Per symbol: **dead** (all seven channels cleared, with the evidence), **live via `<channel>`** (name
the reacher), or **cannot prove** (name the channel that stayed open and what would settle it).
Group deletions that must happen together — a dead function and its dead test — and state explicitly
what you deleted versus what you are only recommending.
