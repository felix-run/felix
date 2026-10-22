"""The subjects a run carries when no person authenticated it: one place for every one of them.

A machine actor is the `principal_sub` of work the harness does itself -- a scheduled job's
firing, a resumed durable run, an A2A call with no caller, an eval item, memory consolidation, the
skill library improving or evaluating a skill --
and `acting_for` falls back to it when that work is for no one. A trigger's run is `trigger:<id>`,
shared by every sender. None of these names a person, which is what `recorded.is_shared_subject`
reads: a tool addressed to "the person this run is for" refuses them.
"""

from __future__ import annotations

CRON = "cron"
FIBER = "fiber"
A2A = "a2a"
EVAL = "eval"
CONSOLIDATION = "consolidation"
# The skill library's own model calls (`skills/improve.py`, `skills/evaluate.py`).
SKILL_IMPROVER = "skill-improver"
SKILL_EVAL = "skill-eval"
# What the auth middleware names a caller that presented nothing.
ANONYMOUS = "anonymous"
MACHINE_ACTORS = frozenset({CRON, FIBER, A2A, EVAL, CONSOLIDATION, SKILL_IMPROVER, SKILL_EVAL, ANONYMOUS})

# A run's `trigger` when a signed trigger started it, and the subject that run acts as: `trigger:<id>`.
TRIGGER_RUN_PREFIX = "trigger:"

__all__ = [
    "A2A",
    "ANONYMOUS",
    "CONSOLIDATION",
    "CRON",
    "EVAL",
    "FIBER",
    "MACHINE_ACTORS",
    "SKILL_EVAL",
    "SKILL_IMPROVER",
    "TRIGGER_RUN_PREFIX",
]
