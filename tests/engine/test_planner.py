"""The adaptive parallelism planner (balamut.engine.planner)."""

from __future__ import annotations

import pytest

from balamut.engine import planner


def test_daemon_plan_scales_with_pending_work_and_isolation() -> None:
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=0, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(0, 1, 0)
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=1, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(1, 1, 1)
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=41, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(2, 1, 2)
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=200, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(4, 1, 4)
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=1000, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(5, 1, 5)
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=1000, perf_cores=10, isolated=True
    ) == planner.ExecutionPlan(5, 1, 5)


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize(("perf_cores", "daemons"), [(1, 1), (2, 1), (3, 1), (6, 3), (10, 5), (12, 6)])
def test_auto_plan_caps_daemons_at_half_the_performance_cores(isolated: bool, perf_cores: int, daemons: int) -> None:
    """A daemon beyond half the cores adds a runtime with its own collection and database clone, not throughput."""
    assert planner.resolve_execution_plan(
        explicit_daemons=0, workers_per_daemon=1, pending=1000, perf_cores=perf_cores, isolated=isolated
    ) == planner.ExecutionPlan(daemons, 1, daemons)


def test_explicit_daemons_disable_heuristic_but_are_clamped_to_work() -> None:
    assert planner.resolve_execution_plan(
        explicit_daemons=8, workers_per_daemon=1, pending=3, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(3, 1, 3)
    # The explicit count overrides the performance-core budget entirely (a larger machine may saturate).
    assert planner.resolve_execution_plan(
        explicit_daemons=20, workers_per_daemon=2, pending=30, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(20, 2, 40)


def test_auto_plan_budgets_total_workers_across_daemons() -> None:
    two_workers = planner.resolve_execution_plan(
        explicit_daemons=0,
        workers_per_daemon=2,
        pending=1000,
        perf_cores=10,
        isolated=False,
    )
    four_workers = planner.resolve_execution_plan(
        explicit_daemons=0,
        workers_per_daemon=4,
        pending=1000,
        perf_cores=10,
        isolated=False,
    )

    assert two_workers == planner.ExecutionPlan(5, 2, 10)
    assert four_workers == planner.ExecutionPlan(3, 4, 12)
    assert two_workers.capacity <= 15
    assert four_workers.capacity <= 15


def test_daemon_plan_rejects_nonpositive_workers() -> None:
    with pytest.raises(ValueError, match="workers_per_daemon"):
        planner.resolve_execution_plan(
            explicit_daemons=0,
            workers_per_daemon=0,
            pending=1,
            perf_cores=10,
            isolated=False,
        )


def test_refresh_plan_uses_benchmark_derived_buckets() -> None:
    assert planner.resolve_refresh_workers(explicit=None, targets=0, perf_cores=10) == 0
    assert planner.resolve_refresh_workers(explicit=None, targets=43, perf_cores=10) == 1
    assert planner.resolve_refresh_workers(explicit=None, targets=200, perf_cores=10) == 2
    assert planner.resolve_refresh_workers(explicit=None, targets=518, perf_cores=10) == 6
    assert planner.resolve_refresh_workers(explicit=None, targets=1, perf_cores=0) == 1


def test_explicit_refresh_workers_are_clamped_to_targets_and_cores() -> None:
    assert planner.resolve_refresh_workers(explicit=8, targets=3, perf_cores=10) == 3
    assert planner.resolve_refresh_workers(explicit=20, targets=30, perf_cores=10) == 10


def test_auto_plan_picks_the_exact_bucket_at_each_pending_boundary() -> None:
    """Each pending-work bucket boundary (64, 256) picks its own pool shape."""
    plan = planner.resolve_execution_plan
    assert plan(explicit_daemons=0, workers_per_daemon=1, pending=64, perf_cores=10, isolated=False) == (
        planner.ExecutionPlan(2, 1, 2)
    )
    assert plan(explicit_daemons=0, workers_per_daemon=1, pending=65, perf_cores=10, isolated=False) == (
        planner.ExecutionPlan(4, 1, 4)
    )
    assert plan(explicit_daemons=0, workers_per_daemon=1, pending=256, perf_cores=10, isolated=False) == (
        planner.ExecutionPlan(4, 1, 4)
    )
    assert plan(explicit_daemons=0, workers_per_daemon=1, pending=257, perf_cores=10, isolated=False) == (
        planner.ExecutionPlan(5, 1, 5)
    )


def test_auto_plan_floors_core_limit_and_budget_limit() -> None:
    """A zero core count floors at one daemon, and a worker budget below the floor stays at one daemon."""
    plan = planner.resolve_execution_plan
    # The explicit override ignores the core count entirely: it is clamped only to the work.
    assert plan(explicit_daemons=2, workers_per_daemon=1, pending=2, perf_cores=0, isolated=False) == (
        planner.ExecutionPlan(2, 1, 2)
    )
    # workers_per_daemon == 6 with 4 perf cores floors the budget at 1 (6 // 6 == 1), so a huge
    # isolated pool stays at one daemon.
    assert plan(explicit_daemons=0, workers_per_daemon=6, pending=1000, perf_cores=4, isolated=True) == (
        planner.ExecutionPlan(1, 6, 6)
    )


def test_a_single_explicit_daemon_is_a_positive_override() -> None:
    """explicit_daemons == 1 is a positive override, distinct from the zero (auto) sentinel."""
    assert planner.resolve_execution_plan(
        explicit_daemons=1, workers_per_daemon=1, pending=100, perf_cores=10, isolated=False
    ) == planner.ExecutionPlan(1, 1, 1)


def test_refresh_workers_are_exact_at_the_bucket_boundaries() -> None:
    """The refresh buckets (64, 256) and the core floor are exact at their boundaries."""
    resolve = planner.resolve_refresh_workers
    assert resolve(explicit=None, targets=64, perf_cores=10) == 1
    assert resolve(explicit=None, targets=65, perf_cores=10) == 2
    assert resolve(explicit=None, targets=256, perf_cores=10) == 2
    assert resolve(explicit=None, targets=257, perf_cores=10) == 6
    # A single core floors the mid bucket at one worker.
    assert resolve(explicit=None, targets=100, perf_cores=1) == 1
    # explicit == 0 is the auto sentinel, not a positive override.
    assert resolve(explicit=0, targets=100, perf_cores=10) == 2
    # explicit == 1 is a positive override, clamped to one worker.
    assert resolve(explicit=1, targets=100, perf_cores=10) == 1
