"""How long a stopping worker is given, and what the time is for.

Taskiq decides two things at shutdown, and both defaults were wrong for this worker. It waits
for tasks still running with no limit (`wait_tasks_timeout=None`), so a `fiber_scheduler` sweep
holding one run parked on an approval kept the process until it was killed. Then it runs the
shutdown handler under `shutdown_timeout`, five seconds by default -- and the handler's first job
is a thirty-second drain. So the handler was cancelled in the drain: no fiber was stopped or gave
its claim back, every run in flight sat out its lease, and the flush behind the drain never ran.

Derived from the pieces rather than written as numbers, because the pieces are what change.
`tests/unit/test_entrypoint_wiring.py` holds the sum inside the grace period Compose and the
Helm chart give the container.
"""

from __future__ import annotations

from felix.durability.fibers import FIBER_DRAIN_BUDGET_S, FIBER_LOOP_DRAIN_S, FIBER_LOOP_STOP_S

# Tasks still running when the worker is told to stop get this long before the handler runs.
# Short: the handler is what drains the fibers a sweep is holding, and it cannot start sooner.
# Taskiq does not cancel a task this runs out on -- it stops waiting, and the task dies with the
# process -- so ending those tasks is the handler's job too (`tasks._stop_cron_tasks`).
WAIT_TASKS_TIMEOUT_S = 5.0
# What a cron task still running gets, in the handler: as long to finish by itself as a fiber
# gets to reach its next suspension, and then as long to act on its cancel as a cancelled fiber
# gets -- a scheduled job records its cancelled run and clears its `running` mark there. The two
# are the fibers' own windows on purpose: the handler runs both drains side by side, so a job
# and a durable run stopped by the same signal share one window and do not queue for two.
CRON_DRAIN_S = FIBER_LOOP_DRAIN_S
CRON_STOP_S = FIBER_LOOP_STOP_S
# Everything the handler waits on before it flushes: the longer of the two drains.
WORK_DRAIN_BUDGET_S = max(FIBER_DRAIN_BUDGET_S, CRON_DRAIN_S + CRON_STOP_S)
# What the flush after the drain may take: it is bounded by the database timeouts, not the drain.
FLUSH_ALLOWANCE_S = 30.0
# Slack over the sum. A handler cancelled *during* the drain's last wait is a second cancel on
# fibers that are giving their claims back, so the timeout must not land anywhere near it.
SHUTDOWN_MARGIN_S = 10.0
# The drains (`tasks._stop_work`), then the flush.
SHUTDOWN_TIMEOUT_S = WORK_DRAIN_BUDGET_S + FLUSH_ALLOWANCE_S + SHUTDOWN_MARGIN_S
# Everything between the stop signal and the process being gone.
SHUTDOWN_BUDGET_S = WAIT_TASKS_TIMEOUT_S + SHUTDOWN_TIMEOUT_S
