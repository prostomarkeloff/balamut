"""Verdict logic of a mutation campaign over scripted daemons (balamut.engine.campaign/worker).

A kill counts only when the killer passes on the immediately restored original; a warm verdict that never entered
the mutated code object is confirmed in a fresh fork; timeouts retry in isolation and only terminal verdicts are
persisted.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from balamut.core.mutants import Mutant
from balamut.coverage.store import Store
from balamut.engine import campaign, partition, timeouts
from balamut.engine.daemon import Daemon, RunResult
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import CampaignExactnessError, CampaignSetupError, MutantResult, MutantStatus
from balamut.engine.worker import Timing
from balamut.isolation import EnvMap, EnvOverlays, Isolation
from balamut.operators.concurrency import LockDrop
from balamut.runtime.plugin import original_lease_deps, parse_mutant_spec
from bm_support.campaign import (
    FakeIsolation,
    missing_costs,
    mutant,
    no_missing_costs,
    no_pool_action,
    options,
    run,
    same_pool,
    stub_campaign_runtime,
    survive_one,
    work,
    workers_count,
)
from bm_support.daemons import fake_daemon

_KILLER = "tests/x_test.py::test_x"


def _runs_the_original(raw: str) -> bool:
    """A paired original runs no mutation; its lease only records its functions into the kill dependencies."""
    return parse_mutant_spec(raw) is None and original_lease_deps(raw) is not None


@pytest.mark.parametrize(
    ("original_result", "fresh_results", "expected", "expected_requests", "expected_resets"),
    [
        (RunResult(0, {}, []), [RunResult(1, {}, [_KILLER]), RunResult(0, {}, [])], MutantStatus.KILLED, 2, 0),
        (
            RunResult(1, {}, [_KILLER]),
            [RunResult(1, {}, [_KILLER]), RunResult(0, {}, [])],
            MutantStatus.KILLED,
            4,
            1,
        ),
        (RunResult(1, {}, [_KILLER]), [RunResult(0, {}, [])], MutantStatus.SURVIVED, 3, 1),
        (RunResult(1, {}, [], process_failures=[_KILLER]), [RunResult(0, {}, [])], MutantStatus.SURVIVED, 3, 1),
        (
            RunResult(1, {}, [_KILLER]),
            [RunResult(1, {}, [_KILLER]), RunResult(1, {}, [_KILLER])],
            MutantStatus.TIMEOUT,
            4,
            1,
        ),
        (None, [], MutantStatus.TIMEOUT, 2, 0),
    ],
)
def test_kill_is_accepted_only_when_killer_passes_on_immediate_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_result: RunResult | None,
    fresh_results: list[RunResult],
    expected: MutantStatus,
    expected_requests: int,
    expected_resets: int,
) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch, tmp_path, [work(0, item)], [RunResult(1, {}, [_KILLER]), original_result, *fresh_results]
    )
    isolation = FakeIsolation()
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, isolation=isolation))
        cached = store.get_verdict("key-0")
        cached_killers = dict(store.killed_with_killers()).get("key-0", [])
    finally:
        store.close()

    assert results[0].status is expected
    assert results[0].killed_by == ((_KILLER,) if expected is MutantStatus.KILLED else ())
    assert stub.persisted == [expected]
    assert [nodeids for nodeids, _raw in stub.requests] == [[_KILLER]] * expected_requests
    assert stub.requests[0][1]
    assert _runs_the_original(stub.requests[1][1])
    # The campaign owns its slots: prepared once, recreated for a fresh confirmation, removed at the end.
    assert len(isolation.prepared) == 1 + expected_resets
    assert isolation.cleaned == [isolation.prepared[0]]
    assert cached is not None
    assert cached_killers == ([_KILLER] if expected is MutantStatus.KILLED else [])


def test_touched_warm_kill_is_terminal_after_green_paired_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch, tmp_path, [work(0, item)], [RunResult(1, {}, [_KILLER]), RunResult(0, {}, [])]
    )
    isolation = FakeIsolation()
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, isolation=isolation))
    finally:
        store.close()

    assert results[0].status is MutantStatus.KILLED
    assert stub.fresh == [False, False]
    assert stub.fresh_transitions == []
    assert len(isolation.prepared) == 1
    assert stub.persisted == [MutantStatus.KILLED]


def test_untouched_kill_requires_fresh_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item)],
        [RunResult(1, {}, [_KILLER]), RunResult(0, {}, []), RunResult(1, {}, [_KILLER]), RunResult(0, {}, [])],
        runtime_touched=[False, False, True, False],
    )

    results = run(tmp_path, [item], opts=options(tmp_path, isolation=FakeIsolation()))

    assert results[0].status is MutantStatus.KILLED
    assert stub.fresh == [False, False, True, True]
    assert len(stub.fresh_transitions) == 1


def test_process_failure_records_first_unreported_test_as_canonical_killer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_a", "tests/x_test.py::test_b")
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item, covering)],
        [RunResult(1, {}, [], process_failures=[covering[0]]), RunResult(0, {}, [])],
    )
    store = Store(tmp_path / "campaign.sqlite")
    detailed_path = tmp_path / "campaign-detailed.json"
    try:
        results = run(
            tmp_path,
            [item],
            store=store,
            opts=options(tmp_path, isolation=FakeIsolation(), detailed=True, detailed_path=detailed_path),
        )
        cached_killers = dict(store.killed_with_killers()).get("key-0", [])
    finally:
        store.close()

    assert results[0].status is MutantStatus.KILLED
    assert results[0].killed_by == (covering[0],)
    assert stub.persisted == [MutantStatus.KILLED]
    assert [nodeids for nodeids, _raw in stub.requests] == [list(covering), [covering[0]]]
    assert stub.fresh == [False, True]
    assert _runs_the_original(stub.requests[1][1])
    assert cached_killers == [covering[0]]
    detailed = detailed_path.read_text(encoding="utf-8")
    assert '"tests_run": 1' in detailed
    assert f'"killer": "{covering[0]}"' in detailed


def test_process_failure_without_active_item_evidence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(1, {}, [])])

    with pytest.raises(CampaignExactnessError, match="active pytest item"):
        run(tmp_path, [item])


def test_process_failure_with_multiple_workers_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_a", "tests/x_test.py::test_b")
    stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item, covering)],
        [RunResult(1, {}, [], process_failures=[covering[0]]), RunResult(0, {}, [])],
    )

    with pytest.raises(CampaignExactnessError, match="canonical killer"):
        run(tmp_path, [item], opts=options(tmp_path, workers=2))


def test_original_process_failure_does_not_confirm_unexecuted_killers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_a", "tests/x_test.py::test_b")
    stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item, covering)],
        [RunResult(1, {}, list(covering)), RunResult(1, {covering[0]: 0.1}, [], process_failures=[covering[1]])],
    )

    results = run(tmp_path, [item], opts=options(tmp_path, cross_kill=True))

    assert results[0].status is MutantStatus.KILLED
    assert results[0].killed_by == (covering[0],)


def test_campaign_uses_coverage_costs_for_timeout_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_x",)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item, covering)], [RunResult(0, {}, [])])

    results = run(tmp_path, [item], initial_costs={covering[0]: 3.0})

    assert results[0].status is MutantStatus.SURVIVED
    assert stub.timeouts == [timeouts.mutant_timeout_budget(8.0, covering, {covering[0]: 3.0})]


def test_current_coverage_cost_replaces_stale_store_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_x",)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item, covering)], [RunResult(0, {}, [])])
    store = Store(tmp_path / "campaign.sqlite")
    store.put_costs({covering[0]: 0.1})
    try:
        run(tmp_path, [item], store=store, initial_costs={covering[0]: 10.0})
        stored = store.get_costs(list(covering))
    finally:
        store.close()

    assert stub.timeouts == [timeouts.mutant_timeout_budget(8.0, covering, {covering[0]: 10.0})]
    assert stored == {covering[0]: 10.0}


def test_cross_kill_keeps_only_killers_that_pass_on_immediate_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    stable = "tests/x_test.py::test_stable"
    unstable = "tests/x_test.py::test_unstable"
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item, (stable, unstable))],
        [RunResult(1, {}, [stable, unstable]), RunResult(1, {}, [unstable])],
    )
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, cross_kill=True))
        cached_killers = dict(store.killed_with_killers()).get("key-0", [])
    finally:
        store.close()

    assert results[0].status is MutantStatus.KILLED
    assert stub.persisted == [MutantStatus.KILLED]
    assert [nodeids for nodeids, _raw in stub.requests] == [[stable, unstable]] * 2
    assert _runs_the_original(stub.requests[1][1])
    assert cached_killers == [stable]


@pytest.mark.parametrize(
    ("confirmation_result", "confirmation_touched", "expected"),
    [
        (RunResult(1, {}, [_KILLER]), False, MutantStatus.KILLED),
        (RunResult(0, {}, []), True, MutantStatus.SURVIVED),
        (RunResult(0, {}, []), False, MutantStatus.UNSUPPORTED),
    ],
)
def test_untouched_warm_survivor_is_confirmed_in_a_fresh_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmation_result: RunResult,
    confirmation_touched: bool,
    expected: MutantStatus,
) -> None:
    """A warm session fixture may have computed an object on the original in an earlier request: a green run
    that never entered the mutated code proves nothing, so it is confirmed in a fork per request."""
    item = mutant("src/x.py", 10)
    responses: list[RunResult | None] = [RunResult(0, {}, []), confirmation_result]
    touched = [False, confirmation_touched]
    if confirmation_result.rc == 1:
        responses.append(RunResult(0, {}, []))
        touched.append(True)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], responses, runtime_touched=touched)
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(
            tmp_path,
            [item],
            store=store,
            sources={"src/x.py": "x = 1\n"},
            opts=options(tmp_path, isolation=FakeIsolation()),
        )
    finally:
        store.close()

    assert results[0].status is expected
    assert stub.spawn_shapes == [(1, 1)]
    assert stub.spawn_persistence == [True]
    assert stub.fresh == [False, True, *([True] if confirmation_result.rc == 1 else [])]
    assert len(stub.fresh_transitions) == 1
    assert stub.persisted == [expected]


@pytest.mark.parametrize(
    ("retry_result", "expected"),
    [(RunResult(1, {}, [_KILLER]), "killed"), (RunResult(0, {}, []), "survived"), (None, "timeout")],
)
def test_timeout_is_retried_serially_and_persists_only_terminal_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_result: RunResult | None, expected: str
) -> None:
    item = mutant("src/x.py", 10)
    responses: list[RunResult | None] = [None, retry_result]
    expected_timeouts = [8.0, 24.0]
    if retry_result is not None and retry_result.rc == 1:
        responses.append(RunResult(0, {}, []))
        expected_timeouts.append(24.0)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], responses)
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, daemons=3, workers=2, timeout_retries=1))
        cached = store.get_verdict("key-0")
    finally:
        store.close()

    assert results[0].status.value == expected
    assert cached is not None
    assert cached["verdict"] == expected
    assert [status.value for status in stub.persisted] == [expected]
    assert stub.spawn_shapes == [(1, 2)]
    assert stub.timeouts == expected_timeouts


def test_isolated_timeouts_are_retried_across_isolated_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutants = [mutant("src/x.py", index + 1) for index in range(3)]
    terminal = RunResult(0, {}, [])
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(index, item) for index, item in enumerate(mutants)],
        [None, None, None, terminal, terminal, terminal],
    )

    results = run(tmp_path, mutants, opts=options(tmp_path, daemons=3, timeout_retries=1, isolation=FakeIsolation()))

    assert [result.status for result in results] == [MutantStatus.SURVIVED] * 3
    assert stub.spawn_shapes == [(3, 1)]
    assert stub.timeouts == [8.0, 8.0, 8.0, 24.0, 24.0, 24.0]


def test_timeout_without_retries_is_terminal_and_never_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None])
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, daemons=2, workers=2))
    finally:
        store.close()

    assert results[0].status.value == "timeout"
    assert [status.value for status in stub.persisted] == ["timeout"]
    assert stub.spawn_shapes == [(1, 2)]


def _concurrency(lineno: int, start_byte: int) -> Mutant:
    return mutant(
        "src/x.py", lineno, operator="concurrency:general:asynclock-drop", start_byte=start_byte, fact=LockDrop()
    )


def test_concurrency_mutants_use_serial_one_worker_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    normal = mutant("src/x.py", 10)
    concurrency = _concurrency(11, 2)
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, normal), work(1, concurrency)],
        [RunResult(0, {}, []), RunResult(0, {}, [])],
    )

    results = run(tmp_path, [normal, concurrency], opts=options(tmp_path, daemons=4, workers=2))

    assert [result.status for result in results] == [MutantStatus.SURVIVED] * 2
    assert stub.spawn_shapes == [(2, 2), (1, 1)]
    campaign_address = stub.lifecycle[0].removeprefix("spawn:")
    assert campaign_address.startswith(f"{tmp_path}/mutate.sock.run-")
    assert stub.lifecycle == [
        f"spawn:{campaign_address}",
        "run:src/x.py:10",
        f"spawn:{campaign_address}.concurrency",
        "run:src/x.py:11",
    ]


def test_concurrency_mutants_use_bounded_parallel_isolated_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutants = [_concurrency(10 + index, index) for index in range(2)]
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(index, item) for index, item in enumerate(mutants)],
        [RunResult(0, {}, []), RunResult(0, {}, [])],
    )

    results = run(tmp_path, mutants, opts=options(tmp_path, daemons=4, isolation=FakeIsolation()))

    assert [result.status for result in results] == [MutantStatus.SURVIVED] * 2
    assert stub.spawn_shapes == [(2, 1)]


def test_campaign_resolves_auto_daemons_after_cache_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutants = [mutant("src/x.py", index + 1) for index in range(41)]
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(index, item) for index, item in enumerate(mutants)],
        [RunResult(0, {}, []) for _item in mutants],
    )

    results = run(tmp_path, mutants, opts=options(tmp_path, daemons=0, workers=2))

    assert len(results) == 41
    assert stub.spawn_shapes == [(2, 2)]


def test_isolated_daemons_get_their_own_slot_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutants = [mutant("src/x.py", index + 1) for index in range(2)]
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(index, item) for index, item in enumerate(mutants)],
        [RunResult(0, {}, []), RunResult(0, {}, [])],
    )
    isolation = FakeIsolation()

    run(tmp_path, mutants, opts=options(tmp_path, daemons=2, isolation=isolation))

    [(count, prefix)] = isolation.prepared
    assert count == 2
    assert stub.spawn_overlays == [tuple({"APP_DATABASE": f"{prefix}_w{index}"} for index in range(2))]
    assert isolation.cleaned == [(2, prefix)]


def test_campaign_scopes_daemon_addresses_per_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    spawned: list[str] = []
    for _ in range(2):
        stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
        run(tmp_path, [item])
        spawned.extend(event.removeprefix("spawn:") for event in stub.lifecycle if event.startswith("spawn:"))

    assert len(spawned) == 2
    assert all(address.startswith(f"{tmp_path}/mutate.sock.run-") for address in spawned)
    assert spawned[0] != spawned[1]


@pytest.mark.parametrize(
    ("mutant_timeout", "retry_multiplier", "retries", "message"),
    [
        (float("nan"), 3.0, 1, "mutant timeout"),
        (0.0, 3.0, 1, "mutant timeout"),
        (8.0, float("inf"), 1, "retry multiplier"),
        (8.0, -1.0, 1, "retry multiplier"),
        (1e308, 10.0, 2, "must stay finite"),
    ],
)
def test_run_campaign_rejects_an_invalid_timeout_policy_before_partition(
    tmp_path: Path, mutant_timeout: float, retry_multiplier: float, retries: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run(
            tmp_path,
            [],
            opts=options(
                tmp_path,
                mutant_timeout=mutant_timeout,
                timeout_retry_multiplier=retry_multiplier,
                timeout_retries=retries,
            ),
        )


def _survive(item: partition.Work, results: partition.ResultsByIndex, progress: CampaignProgress) -> None:
    result = MutantResult(item.mutant, MutantStatus.SURVIVED, len(item.covering), item.key, item.covering)
    results[item.index] = result
    progress.advance("test", result.status, result.key)


def _lane(
    tmp_path: Path,
    items: list[partition.Work],
    pool: list[Daemon],
    results: partition.ResultsByIndex | None = None,
    *,
    isolation: Isolation | None = None,
) -> None:
    campaign.run_lane(
        items,
        {} if results is None else results,
        CampaignProgress(len(items), ProgressMode.NONE),
        Timing(),
        {},
        None,
        pool=pool,
        address_base=str(tmp_path / "mutate.sock"),
        scratch=tmp_path / "run",
        cross_kill=False,
        mutant_timeout=8.0,
        finalize_timeout=True,
        isolation=isolation,
    )


def test_run_lane_calibrates_on_a_disposable_wide_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    # Five lane daemons but only two missing tests: the wide calibration workers follow ``missing``, not the pool
    # size, so swapping the two arguments would over-spawn.
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(5)]
    spawned: list[tuple[str, int, int, EnvOverlays]] = []
    shutdowns: list[list[str]] = []

    def fake_spawn_pool(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kwargs: object
    ) -> list[Daemon]:
        _ = env
        spawned.append((address_base, daemons, workers, overlays))
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=10 + index) for index in range(daemons)]

    def fake_calibrate(calibration: list[Daemon], _work: list[partition.Work], costs: dict[str, float]) -> list[Daemon]:
        assert [daemon.address for daemon in calibration] == [f"{tmp_path}/mutate.sock.calibration.0"]
        costs.update({"test_a": 0.1, "test_b": 0.2})
        return calibration

    monkeypatch.setattr(campaign, "resolve_workers", workers_count(8))
    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs("test_a", "test_b"))
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn_pool)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", fake_calibrate)
    monkeypatch.setattr(campaign, "worker_loop", survive_one)

    def record_shutdown(target: list[Daemon]) -> None:
        shutdowns.append([daemon.address for daemon in target])

    monkeypatch.setattr(campaign, "shutdown_pool", record_shutdown)
    results: partition.ResultsByIndex = {}

    _lane(tmp_path, [item], pool, results)

    assert spawned == [(f"{tmp_path}/mutate.sock.calibration", 1, 2, ())]
    assert shutdowns == [[f"{tmp_path}/mutate.sock.calibration.0"]]
    assert results[0].status is MutantStatus.SURVIVED


def test_isolated_run_lane_calibrates_on_its_own_narrow_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared database cannot take a wide parallel baseline: each calibration daemon gets a slot of its own."""
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    spawned: list[tuple[int, int, EnvOverlays]] = []
    isolation = FakeIsolation()

    def fake_spawn_pool(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kwargs: object
    ) -> list[Daemon]:
        _ = (env, address_base)
        spawned.append((daemons, workers, overlays))
        return [fake_daemon(tmp_path, address=f"cal-{index}", index=10 + index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs("test_a", "test_b"))
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn_pool)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", same_pool)
    monkeypatch.setattr(campaign, "worker_loop", survive_one)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)

    _lane(tmp_path, [item], pool, isolation=isolation)

    [(count, prefix)] = isolation.prepared
    assert count == 2
    assert spawned == [(2, 1, tuple({"APP_DATABASE": f"{prefix}_w{index}"} for index in range(2)))]
    assert isolation.cleaned == [(2, prefix)]


def test_run_lane_computes_missing_costs_with_real_work_and_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The un-mocked ``missing_cost_nodeids(work, costs)`` finds the covering test absent from the empty cost map,
    so a calibration spawn happens; swapping the arguments would see no missing test and skip calibration."""
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path, address="sock-0")]
    spawned: list[tuple[str, int, int]] = []

    def fake_spawn_pool(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays)
        spawned.append((address_base, daemons, workers))
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=10 + index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "resolve_workers", workers_count(8))
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn_pool)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", same_pool)
    monkeypatch.setattr(campaign, "worker_loop", survive_one)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)

    results: partition.ResultsByIndex = {}
    _lane(tmp_path, [item], pool, results)

    assert spawned == [(f"{tmp_path}/mutate.sock.calibration", 1, 1)]
    assert results[0].status is MutantStatus.SURVIVED


def test_run_lane_drains_a_late_requeue_after_other_workers_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = work(0, mutant("src/x.py", 10))
    first_attempt_started = threading.Event()
    failed_once = threading.Event()
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]

    def fake_worker_loop(
        target: Daemon,
        pending: queue.Queue[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        if target.address == "sock-1" and not failed_once.is_set():
            first_attempt_started.wait(timeout=1.0)
        try:
            taken = pending.get_nowait()
        except queue.Empty:
            return
        if target.address == "sock-0" and not failed_once.is_set():
            first_attempt_started.set()
            time.sleep(0.05)  # the other worker sees an empty queue and exits
            failed_once.set()
            pending.put(taken)
            return
        _survive(taken, results, progress)

    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", fake_worker_loop)
    results: partition.ResultsByIndex = {}

    _lane(tmp_path, [item], pool, results)

    assert failed_once.is_set()
    assert results[0].status is MutantStatus.SURVIVED


def test_run_lane_bounds_a_permanently_requeued_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    attempts = 0

    def fake_worker_loop(
        _daemon: Daemon, pending: queue.Queue[partition.Work], *_args: object, **_kwargs: object
    ) -> None:
        nonlocal attempts
        try:
            taken = pending.get_nowait()
        except queue.Empty:
            return
        attempts += 1
        pending.put(taken)

    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", fake_worker_loop)

    with pytest.raises(RuntimeError, match="after 2 drain waves"):
        _lane(tmp_path, [item], [fake_daemon(tmp_path)])

    assert attempts == 2


type _RequestCall = tuple[str, list[str] | None, bool]


def _calibration_request(
    calls: list[_RequestCall], seconds: float = 0.25, *, fill: tuple[str, ...] = ()
) -> Callable[..., int]:
    def fake_request(
        address: str,
        nodeids: list[str] | None,
        durations: dict[str, float],
        _failures: list[str],
        _progress_clock: list[float] | None = None,
        _phases: dict[str, dict[str, float]] | None = None,
        *,
        stop_on_failure: bool,
        fresh_workers: bool = False,
    ) -> int:
        assert fresh_workers is False
        calls.append((address, nodeids, stop_on_failure))
        durations.update(dict.fromkeys(nodeids if nodeids is not None else fill, seconds))
        return 0

    return fake_request


def test_calibrate_costs_runs_each_missing_covering_test_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path)]
    costs: dict[str, float] = {}

    assert campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10))], costs) == pool
    assert calls == [("sock", ["tests/x_test.py::test_x"], False)]
    assert costs == {"tests/x_test.py::test_x": 0.25}


def test_calibrate_costs_shards_missing_tests_across_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    costs: dict[str, float] = {}

    assert campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], costs) == pool
    requested = sorted(nodeid for _address, shard, _stop in calls for nodeid in shard or [])
    assert requested == sorted(nodeids)
    assert max(sum(1 for address, _shard, _stop in calls if address == d.address) for d in pool) > 1
    assert costs == dict.fromkeys(nodeids, 0.25)


def test_calibrate_costs_uses_one_request_for_wide_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    costs: dict[str, float] = {}

    campaign.calibrate_costs([fake_daemon(tmp_path, workers=8)], [work(0, mutant("src/x.py", 10), nodeids)], costs)

    assert calls == [("sock", sorted(nodeids), False)]
    assert costs == dict.fromkeys(nodeids, 0.25)


def test_calibrate_costs_preserves_collection_order_for_wide_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(campaign.COST_CALIBRATION_FULL_SUITE_MIN))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls, fill=nodeids))
    costs: dict[str, float] = {}

    campaign.calibrate_costs([fake_daemon(tmp_path, workers=8)], [work(0, mutant("src/x.py", 10), nodeids)], costs)

    assert calls == [("sock", None, False)]
    assert costs == dict.fromkeys(nodeids, 0.25)


def test_calibrate_costs_skips_known_covering_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*_args: object, **_kwargs: object) -> int:
        pytest.fail("known costs must not trigger calibration")

    monkeypatch.setattr(campaign, "request", fail_request)
    costs = {"tests/x_test.py::test_x": 0.25}

    assert campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10))], costs) == []
    assert costs == {"tests/x_test.py::test_x": 0.25}


def test_red_calibration_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def red(
        _address: str, _nodeids: list[str] | None, _durations: dict[str, float], failures: list[str], **_kwargs: bool
    ) -> int:
        failures.append("tests/x_test.py::test_x")
        return 1

    monkeypatch.setattr(campaign, "request", red)

    with pytest.raises(RuntimeError, match="baseline cost calibration is not green"):
        campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10))], {})


def test_daemons_collect_only_covering_test_files_with_matching_client_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client and the daemons must agree on PYTEST_ADDOPTS: pytest-fast fingerprints it."""
    item = mutant("src/x.py", 10)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p balamut.runtime.plugin")
    covering = ("tests/b/b_test.py::test_b[param]", "tests/a_test.py::test_a", "tests/a_test.py::TestX::test_c")
    seen: list[tuple[str, str]] = []

    def observe(target: Daemon) -> None:
        seen.append((target.env["PYTEST_ADDOPTS"], os.environ["PYTEST_ADDOPTS"]))

    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item, covering)], [RunResult(0, {}, [])], observe=observe)

    run(tmp_path, [item])

    expected = "-p balamut.runtime.plugin -o faulthandler_timeout=8 tests/a_test.py tests/b/b_test.py"
    assert seen == [(expected, expected)]
    assert os.environ["PYTEST_ADDOPTS"] == "-p balamut.runtime.plugin"


def test_persist_pool_adopts_at_a_fixed_address_then_spawns_on_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted campaign probes the fixed pool address first; a miss falls back to the fresh spawn."""
    item = mutant("src/x.py", 10)
    record = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])

    results = run(tmp_path, [item], opts=options(tmp_path, persist_pool=True))

    assert results[0].status is MutantStatus.SURVIVED
    assert record.lifecycle[0] == f"adopt:{tmp_path / 'mutate.sock'}.pool"
    assert record.lifecycle[1].startswith("spawn:")


def test_persist_pool_rejects_an_isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted daemons would reference slots that die with the campaign: the combination fails loud."""
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])

    with pytest.raises(CampaignSetupError, match="--persist-pool needs a shared"):
        run(tmp_path, [item], opts=options(tmp_path, persist_pool=True, isolation=FakeIsolation()))
