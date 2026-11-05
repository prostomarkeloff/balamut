"""Adaptive parallelism for a mutation campaign and for a coverage refresh."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The shape of the mutation pool and the total number of pytest workers."""

    daemons: int
    workers_per_daemon: int
    capacity: int


def resolve_execution_plan(
    *,
    explicit_daemons: int,
    workers_per_daemon: int,
    pending: int,
    perf_cores: int,
    isolated: bool,
) -> ExecutionPlan:
    """Choose the pool shape under one CPU budget for daemons and their workers.

    The automatic mode is capped at 1.5x the performance cores (a little headroom hides I/O waits) for daemons and
    nested workers together, and at half the performance cores for daemons: each daemon holds its own Python runtime
    with its own collection (its forkserver idles while the worker runs), its own database copy and connection pool,
    while the database, memory and cores are shared. Measured on six performance cores, six daemons against three
    gave the same wall time per gate, but each request ran almost twice as long and peak memory almost doubled. An
    explicit daemon count is an intentional override, clamped only to the work: a machine with more cores may
    saturate beyond the performance-core count."""
    if workers_per_daemon < 1:
        raise ValueError("workers_per_daemon must be positive")
    if pending <= 0:
        return ExecutionPlan(0, workers_per_daemon, 0)
    core_limit = max(1, perf_cores)
    if explicit_daemons > 0:
        daemons = min(explicit_daemons, pending)
        return ExecutionPlan(daemons, workers_per_daemon, daemons * workers_per_daemon)
    worker_budget = math.ceil(core_limit * 1.5)
    budget_limit = max(1, worker_budget // workers_per_daemon)
    daemon_limit = max(1, core_limit // 2)
    limit = min(pending, daemon_limit, budget_limit)
    if not isolated and pending <= 64:
        daemons = min(2, limit)
    elif not isolated and pending <= 256:
        daemons = min(4, limit)
    else:
        daemons = limit
    return ExecutionPlan(daemons, workers_per_daemon, daemons * workers_per_daemon)


def resolve_refresh_workers(*, explicit: int | None, targets: int, perf_cores: int) -> int:
    """Workers of a coverage refresh, bucketed by the number of targets."""
    if targets <= 0:
        return 0
    limit = max(1, min(targets, perf_cores))
    if explicit is not None and explicit > 0:
        return min(explicit, limit)
    if targets <= 64:
        return 1
    if targets <= 256:
        return min(2, limit)
    return min(6, limit)
