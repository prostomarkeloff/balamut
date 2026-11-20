"""Cache partition, persistence, the stats snapshot of a run, the scope watch and the detailed report."""

from __future__ import annotations

import json
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from balamut.core.hashing import file_hash, mutant_key, sha
from balamut.core.mutants import Mutant, apply_mutant
from balamut.core.triage import Bucket
from balamut.coverage.coverage_map import CoverageMap
from balamut.coverage.store import Store
from balamut.diff.scope import ScopeWatch
from balamut.engine import campaign, identify, partition, stats, worker
from balamut.engine.daemon import RunResult
from balamut.engine.output import result_json
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import (
    CampaignExactnessError,
    CampaignScopeChangedError,
    DaemonStaleError,
    MutantResult,
    MutantStatus,
)
from balamut.engine.worker import Diagnostics, MutantDiag, Timing
from balamut.operators.concurrency import LockDrop
from balamut.operators.oracle import OracleFact
from balamut.operators.src import enumerate_mutants
from balamut.report.agent import records_from_store
from balamut.runtime.kill_deps import kill_deps_encode
from bm_support import make_layout, write_file
from bm_support.campaign import (
    FixedCoverage,
    fixed_support_hash,
    mutant,
    no_missing_costs,
    no_pool_action,
    options,
    run,
    stub_campaign_runtime,
    work,
)
from bm_support.daemons import fake_daemon
from bm_support.scopes import pinned

type StatsPayload = dict[str, stats.JsonValue]


def _payload(path: Path) -> StatsPayload:
    payload: stats.JsonValue = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _setup_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Mutant]:
    source = "def valid(timeout: int) -> bool:\n    return timeout <= 0\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_valid():\n    assert True\n")
    item = next(
        m
        for m in enumerate_mutants("src/x.py", source)
        if m.operator.startswith("lang:const:num:") and m.original == "0"
    )
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    return source, item


def _partition(
    tmp_path: Path, item: Mutant, source: str, store: Store, coverage: FixedCoverage, *, read_cache: bool
) -> tuple[list[partition.Work], partition.ResultsByIndex, int]:
    return partition.partition(
        [item],
        {item.path: source},
        coverage,
        layout=make_layout(tmp_path),
        store=store,
        read_cache=read_cache,
        memo={},
        reexport={},
    )


def test_partition_counts_cache_hits_and_always_reruns_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    coverage = FixedCoverage()
    store = Store(tmp_path / "cache.sqlite")
    try:
        first, _prefilled, first_hits = _partition(tmp_path, item, source, store, coverage, read_cache=False)
        survived = MutantResult(item, MutantStatus.SURVIVED, 1, first[0].key, first[0].covering)
        partition.persist_verdict(store, first[0], survived, [])
        second, second_prefilled, second_hits = _partition(tmp_path, item, source, store, coverage, read_cache=True)
        timed_out = MutantResult(item, MutantStatus.TIMEOUT, 1, first[0].key, first[0].covering)
        partition.persist_verdict(store, first[0], timed_out, [])
        third, third_prefilled, third_hits = _partition(tmp_path, item, source, store, coverage, read_cache=True)
    finally:
        store.close()

    assert first_hits == 0
    assert second == []
    assert second_prefilled[0].status is MutantStatus.SURVIVED
    assert second_hits == 1
    assert [work_item.index for work_item in third] == [0]
    assert third_prefilled == {}
    assert third_hits == 0


def test_a_covered_file_edit_invalidates_the_cached_survivor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    coverage = FixedCoverage({"tests/x_test.py::test_valid"})
    store = Store(tmp_path / "cache.sqlite")
    try:
        first, _prefilled, _hits = _partition(tmp_path, item, source, store, coverage, read_cache=False)
        partition.persist_verdict(store, first[0], MutantResult(item, MutantStatus.SURVIVED, 1, first[0].key), [])
        write_file(tmp_path / "tests/x_test.py", "def test_valid():\n    assert not False\n")
        rerun, _prefilled, hits = _partition(tmp_path, item, source, store, coverage, read_cache=True)
    finally:
        store.close()

    assert hits == 0
    assert len(rerun) == 1


def test_identical_functions_in_two_modules_keep_a_verdict_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same function text in two modules: one shared row let each copy overwrite the other, so a warm rerun
    re-ran every copy but the last written one and the report showed one survivor for all of them."""
    source, _item = _setup_source(tmp_path, monkeypatch)
    sources = {"src/x.py": source, "src/copy.py": source + "\n\ndef extra() -> int:\n    return 1\n"}
    write_file(tmp_path / "src/copy.py", sources["src/copy.py"])
    items = [
        next(m for m in enumerate_mutants(path, text) if m.operator.startswith("lang:const:num:") and m.original == "0")
        for path, text in sources.items()
    ]
    coverage = FixedCoverage({"tests/x_test.py::test_valid"})
    layout = make_layout(tmp_path)
    store = Store(tmp_path / "cache.sqlite")
    try:
        first, _prefilled, _hits = partition.partition(
            items, sources, coverage, layout=layout, store=store, read_cache=False, memo={}, reexport={}
        )
        for item in first:
            partition.persist_verdict(store, item, MutantResult(item.mutant, MutantStatus.SURVIVED, 1, item.key), [])
        rerun, _prefilled, hits = partition.partition(
            items, sources, coverage, layout=layout, store=store, read_cache=True, memo={}, reexport={}
        )
        records = records_from_store(store, sorted(sources), {item.key for item in first})
    finally:
        store.close()

    assert len({item.key for item in first}) == 2
    assert (rerun, hits) == ([], 2)
    assert sorted(record["file"] for record in records) == ["src/copy.py", "src/x.py"]


def test_fact_declared_bucket_reaches_work_store_and_raw_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=False
        )
        assert items[0].bucket is Bucket.REVIEW
        result = MutantResult(item, MutantStatus.SURVIVED, 1, items[0].key, items[0].covering)
        partition.persist_verdict(store, items[0], result, [])

        verdict = store.get_verdict(items[0].key)
        assert verdict is not None
        assert verdict["bucket"] == "review"
        assert result_json(result)["bucket"] == "review"
    finally:
        store.close()


def test_cache_hit_refreshes_the_fact_bucket_without_rerunning_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    coverage = FixedCoverage({"tests/x_test.py::test_valid"})
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(tmp_path, item, source, store, coverage, read_cache=False)
        cached = items[0]
        store.put_verdict(
            key=cached.key,
            file=item.path,
            lineno=item.lineno,
            operator=item.operator,
            original=item.original,
            mutated=item.mutated,
            qualname=cached.qualname,
            func_hash=cached.func_hash,
            verdict="killed",
            bucket="review",
            covering=list(cached.covering),
            covering_hash=cached.covering_hash,
            killed_by=["tests/x_test.py::test_valid"],
        )

        rerun, prefilled, hits = _partition(tmp_path, item, source, store, coverage, read_cache=True)

        assert rerun == []
        assert prefilled[0].status is MutantStatus.KILLED
        assert prefilled[0].killed_by == ("tests/x_test.py::test_valid",)
        assert hits == 1
        verdict = store.get_verdict(cached.key)
        assert verdict is not None
        assert verdict["bucket"] == "review"
    finally:
        store.close()


def test_no_coverage_persistence_uses_the_fact_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _items, results, _hits = _partition(tmp_path, item, source, store, FixedCoverage(set()), read_cache=False)

        assert results[0].status is MutantStatus.NO_COVERAGE
        verdict = store.get_verdict(results[0].key)
        assert verdict is not None
        assert verdict["bucket"] == "review"
    finally:
        store.close()


def test_module_level_mutant_is_unsupported_with_its_covering_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_source(tmp_path, monkeypatch)
    source = "LIMIT = 30\n"
    item = next(m for m in enumerate_mutants("src/x.py", source) if m.operator == "lang:const:num:30->31")
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, results, _hits = _partition(tmp_path, item, source, store, FixedCoverage(), read_cache=False)
    finally:
        store.close()

    assert items == []
    assert results[0].status is MutantStatus.UNSUPPORTED
    assert results[0].covering_tests == ("tests/x_test.py::test_x",)


def test_oracle_mutant_is_covered_by_its_own_collected_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    source = "import pytest\n\n@pytest.mark.parametrize('v', [1, 2])\ndef test_x(v):\n    assert v > 0\n"
    write_file(tmp_path / "tests/x_test.py", source)
    item = next(m for m in enumerate_mutants("tests/x_test.py", source) if m.operator.startswith("lang:compare"))
    oracle = Mutant(
        item.path,
        item.lineno,
        item.start_byte,
        item.end_byte,
        item.original,
        item.mutated,
        "lang:assert:negate",
        fact=OracleFact(),
        family="oracle",
    )
    collected = frozenset({"tests/x_test.py::test_x[1]", "tests/x_test.py::test_x[2]", "tests/x_test.py::test_other"})
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _results, _hits = partition.partition(
            [oracle],
            {oracle.path: source},
            FixedCoverage(set()),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
            collected_test_nodeids=collected,
        )
    finally:
        store.close()

    assert items[0].covering == ("tests/x_test.py::test_x[1]", "tests/x_test.py::test_x[2]")


def _oracles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, source: str, *, predicate: str
) -> list[Mutant]:
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    write_file(tmp_path / path, source)
    return [
        Mutant(
            item.path,
            item.lineno,
            item.start_byte,
            item.end_byte,
            item.original,
            item.mutated,
            "lang:assert:negate",
            fact=OracleFact(),
            family="oracle",
        )
        for item in enumerate_mutants(path, source)
        if item.operator.startswith(predicate)
    ]


def test_module_level_oracle_mutants_are_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A module-level oracle has no enclosing function to patch: it is unsupported before any collect-file lookup.
    source = "A = 30\nB = 40\n"
    oracles = _oracles(tmp_path, monkeypatch, "tests/x_test.py", source, predicate="lang:const:num:")
    assert len(oracles) == 4  # each literal shifted up and down
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, results, _hits = partition.partition(
            oracles,
            {oracles[0].path: source},
            FixedCoverage(set()),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert items == []
    assert len(results) == 4
    assert all(result.status is MutantStatus.UNSUPPORTED for result in results.values())
    assert all(result.covering == 0 for result in results.values())


def test_oracle_mutants_in_an_uncollected_defining_file_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A defining file (not a name-matched test file) with no wrapper re-exporting it collects nowhere: unsupported.
    source = "def test_wave():\n    assert True\n\n\ndef test_surge():\n    assert True\n"
    oracles = _oracles(tmp_path, monkeypatch, "tests/pkg/waves.py", source, predicate="lang:const:bool:")
    assert len(oracles) == 2
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, results, _hits = partition.partition(
            oracles,
            {oracles[0].path: source},
            FixedCoverage(set()),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert items == []
    assert len(results) == 2
    assert all(result.status is MutantStatus.UNSUPPORTED for result in results.values())
    assert all(result.covering == 0 for result in results.values())


def test_persist_final_keeps_test_hashes_and_records_source_hashes(tmp_path: Path) -> None:
    store = Store(tmp_path / "cache.sqlite")
    try:
        store.ingest_coverage([("tests/x_test.py::test_x", "src/x.py", 1)])
        store.put_file_hash("tests/x_test.py", "test-hash", "test")
        partition.persist_final(
            store,
            {"src/x.py": "def x():\n    return 1\n", "tests/x_test.py": "def test_x():\n    assert True\n"},
            {"tests/x_test.py::test_x": 0.5},
            layout=make_layout(tmp_path),
        )

        assert store.get_file_hashes("test") == {"tests/x_test.py": "test-hash"}
        assert set(store.get_file_hashes("src")) == {"src/x.py"}
        assert store.get_costs(["tests/x_test.py::test_x"]) == {"tests/x_test.py::test_x": 0.5}
    finally:
        store.close()


def test_hang_detection_bumps_the_published_total_cost_before_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    stats_path = tmp_path / "campaign-stats.json"
    on_retry_total: list[float] = []
    calls = 0

    def capture_on_retry() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            snapshot = stats.read_campaign_stats(stats_path)
            assert snapshot is not None
            on_retry_total.append(snapshot.total_cost)

    stub_campaign_runtime(
        monkeypatch, tmp_path, [work(0, item)], [None, RunResult(0, {}, [])], before_response=capture_on_retry
    )
    run(tmp_path, [item], opts=options(tmp_path, timeout_retries=1, stats_path=stats_path))

    # Without a store the covering weight is one test at the mean cost 1.0; the hang budget is 8 + 8 * 3 = 32.
    assert on_retry_total == [32.0]


def test_stats_snapshot_starts_with_exact_partition_and_finishes_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed, cached, uncovered = mutant("src/x.py", 10), mutant("src/x.py", 11), mutant("src/x.py", 12)
    prefilled = {
        1: MutantResult(cached, MutantStatus.KILLED, 1, "cached-key"),
        2: MutantResult(uncovered, MutantStatus.NO_COVERAGE, 0, "nocov-key"),
    }
    stats_path = tmp_path / "campaign-stats.json"
    initial: list[StatsPayload] = []
    stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, executed)],
        [RunResult(0, {}, [])],
        prefilled=prefilled,
        cache_hits=1,
        before_response=lambda: initial.append(_payload(stats_path)),
    )

    results = run(
        tmp_path, [executed, cached, uncovered], read_cache=True, opts=options(tmp_path, stats_path=stats_path)
    )

    assert [result.status.value for result in results] == ["survived", "killed", "no_coverage"]
    assert initial == [
        {
            "schema_version": 1,
            "complete": False,
            "enumerated": 3,
            "cache_resolved": 1,
            "executed": 1,
            "terminal": 0,
            "status_counts": {"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 1, "unsupported": 0},
            "total_cost": 1.0,
            "done_cost": 0.0,
            "timings": dict.fromkeys(stats.CAMPAIGN_TIMING_KEYS, 0.0),
        }
    ]
    final = stats.read_campaign_stats(stats_path)
    assert final is not None
    assert final.complete is True
    assert final.terminal == 1
    assert final.status_counts == {"killed": 1, "survived": 1, "timeout": 0, "no_coverage": 1, "unsupported": 0}


def test_repeated_cached_timeout_advances_terminal_when_the_store_row_does_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    work_item = work(0, item)
    store = Store(tmp_path / "campaign.sqlite")
    partition.persist_verdict(
        store, work_item, MutantResult(item, MutantStatus.TIMEOUT, 1, work_item.key, work_item.covering), []
    )
    stats_path = tmp_path / "campaign-stats.json"
    stub_campaign_runtime(monkeypatch, tmp_path, [work_item], [None])
    try:
        run(tmp_path, [item], store=store, read_cache=True, opts=options(tmp_path, stats_path=stats_path))
        assert store.count_mutants() == 1
    finally:
        store.close()

    final = stats.read_campaign_stats(stats_path)
    assert final is not None
    assert final.terminal == 1
    assert final.status_counts["timeout"] == 1


@pytest.mark.parametrize(
    ("cached_status", "run_result", "runtime_unsupported", "expected_status"),
    [
        ("unsupported", RunResult(0, {}, []), True, "unsupported"),
        ("survived", RunResult(1, {}, ["tests/x_test.py::test_x"]), False, "killed"),
    ],
)
def test_same_and_compensating_store_verdicts_have_exact_current_run_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cached_status: str,
    run_result: RunResult,
    runtime_unsupported: bool,
    expected_status: str,
) -> None:
    item = mutant("src/x.py", 10)
    work_item = work(0, item)
    store = Store(tmp_path / "campaign.sqlite")
    partition.persist_verdict(
        store, work_item, MutantResult(item, MutantStatus(cached_status), 1, work_item.key, work_item.covering), []
    )
    stats_path = tmp_path / "campaign-stats.json"
    responses = [run_result]
    unsupported = [runtime_unsupported]
    if run_result.rc == 1:
        responses.extend((RunResult(0, {}, []), run_result, RunResult(0, {}, [])))
        unsupported.extend((False, False, False))
    stub_campaign_runtime(monkeypatch, tmp_path, [work_item], responses, runtime_unsupported=unsupported)
    try:
        run(tmp_path, [item], store=store, opts=options(tmp_path, stats_path=stats_path))
        assert store.count_mutants() == 1
    finally:
        store.close()

    final = stats.read_campaign_stats(stats_path)
    assert final is not None
    assert final.terminal == 1
    assert final.status_counts[expected_status] == 1
    assert sum(final.status_counts.values()) == 1


def test_zero_work_campaign_publishes_complete_phase_timings(tmp_path: Path) -> None:
    stats_path = tmp_path / "campaign-stats.json"

    run(tmp_path, [], opts=options(tmp_path, daemons=0, stats_path=stats_path))

    snapshot = stats.read_campaign_stats(stats_path)
    assert snapshot is not None
    assert snapshot.complete is True
    assert set(snapshot.timings) == set(stats.CAMPAIGN_TIMING_KEYS)


def _drain(
    tmp_path: Path, pending: queue.Queue[partition.Work], watch: ScopeWatch
) -> queue.Queue[CampaignExactnessError]:
    fatal: queue.Queue[CampaignExactnessError] = queue.Queue()
    worker.worker_loop(
        fake_daemon(tmp_path),
        pending,
        {},
        CampaignProgress(pending.qsize(), ProgressMode.NONE),
        Timing(),
        {},
        None,
        cross_kill=False,
        mutant_timeout=8.0,
        finalize_timeout=True,
        fatal_errors=fatal,
        scope_watch=watch,
    )
    return fatal


def test_worker_stops_at_the_mutant_boundary_once_the_scope_changed(tmp_path: Path) -> None:
    edited = write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    watch = ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path)
    edited.write_text("X = 0\n", encoding="utf-8")
    pending: queue.Queue[partition.Work] = queue.Queue()
    pending.put(work(0, mutant("src/x.py", 2)))
    pending.put(work(1, mutant("src/x.py", 2)))

    fatal = _drain(tmp_path, pending, watch)

    error = fatal.get_nowait()
    assert isinstance(error, CampaignScopeChangedError)
    assert error.paths == ("src/x.py",)
    assert pending.qsize() == 2, "no mutant runs after the scope changed"


def test_stale_daemon_after_a_scope_edit_aborts_the_campaign_instead_of_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    watch = ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path, interval=3600.0)

    def stale_after_edit(*_args: object, **_kwargs: object) -> RunResult:
        edited.write_text("X = 0\n", encoding="utf-8")
        raise DaemonStaleError("stale")

    monkeypatch.setattr(worker, "run_subset_guarded", stale_after_edit)
    pending: queue.Queue[partition.Work] = queue.Queue()
    pending.put(work(0, mutant("src/x.py", 2)))

    fatal = _drain(tmp_path, pending, watch)

    assert isinstance(fatal.get_nowait(), CampaignScopeChangedError)
    assert pending.empty(), "a mutant of mixed revisions does not go back to the queue"


def test_stale_daemon_with_intact_scope_requeues_the_mutant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    watch = ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path)

    def stale(*_args: object, **_kwargs: object) -> RunResult:
        raise DaemonStaleError("stale")

    monkeypatch.setattr(worker, "run_subset_guarded", stale)
    pending: queue.Queue[partition.Work] = queue.Queue()
    pending.put(work(0, mutant("src/x.py", 2)))

    fatal = _drain(tmp_path, pending, watch)

    assert fatal.empty()
    assert pending.qsize() == 1


def test_lane_raises_scope_change_reported_by_its_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    edited = write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    watch = ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path)
    edited.write_text("X = 0\n", encoding="utf-8")
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)

    with pytest.raises(CampaignScopeChangedError):
        campaign.run_lane(
            [work(0, mutant("src/x.py", 2))],
            {},
            CampaignProgress(1, ProgressMode.NONE),
            Timing(),
            {},
            None,
            pool=[fake_daemon(tmp_path)],
            address_base=str(tmp_path / "mutate.sock"),
            scratch=tmp_path / "run",
            cross_kill=False,
            mutant_timeout=8.0,
            finalize_timeout=True,
            scope_watch=watch,
        )


def _one_row_diags() -> Diagnostics:
    diags = Diagnostics()
    diags.add(
        MutantDiag(
            key="k",
            path="src/x.py",
            lineno=1,
            operator="lang:const:num:1->2",
            status="killed",
            covering=3,
            tests_run=1,
            setup_s=0.1,
            call_s=0.2,
            teardown_s=0.0,
            request_s=0.4,
            write_s=0.01,
            killer="tests/x_test.py::test_x",
        )
    )
    return diags


def test_report_detailed_bad_json_path_is_advisory_not_fatal(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--detailed is a measurement, not a gate: a broken output path must not abort a successful campaign."""
    bad = tmp_path / "missing" / "detailed.json"

    campaign.report_detailed(_one_row_diags(), bad)

    err = capsys.readouterr().err
    assert "--detailed-json was not written" in err
    assert "cost breakdown" in err
    assert not bad.exists()


def test_report_detailed_good_json_path_writes_payload(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    good = tmp_path / "detailed.json"

    campaign.report_detailed(_one_row_diags(), good)

    assert json.loads(good.read_text(encoding="utf-8"))[0]["killer"] == "tests/x_test.py::test_x"
    assert "was not written" not in capsys.readouterr().err


def test_support_corpus_hash_includes_test_support_files(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/helpers.py", "def helper():\n    return 1\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    layout = make_layout(tmp_path)
    with_helper = partition.support_corpus_hash(layout, {})
    (tmp_path / "tests/helpers.py").unlink()
    without_helper = partition.support_corpus_hash(layout, {})
    assert with_helper != without_helper


def test_support_corpus_hash_reads_each_file_once_per_memo(tmp_path: Path) -> None:
    helper = write_file(tmp_path / "tests/helpers.py", "def helper():\n    return 1\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    layout = make_layout(tmp_path)
    memo: dict[str, str] = {}

    first = partition.support_corpus_hash(layout, memo)

    assert memo == {"tests/helpers.py": file_hash(helper)}
    # The memo is the campaign's snapshot of the tree: an edit after the first read does not move the hash.
    helper.write_text("def helper():\n    return 2\n", encoding="utf-8")
    assert partition.support_corpus_hash(layout, memo) == first


def test_no_coverage_mutants_all_reach_the_prefilled_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "def f(a, b):\n    return (a + 1, b + 2)\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    items = [m for m in enumerate_mutants("src/x.py", source) if m.operator.startswith("lang:const:num:")]
    assert len(items) >= 2
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    coverage = FixedCoverage(set())
    store = Store(tmp_path / "cache.sqlite")
    try:
        work_items, prefilled, _hits = partition.partition(
            items,
            {"src/x.py": source},
            coverage,
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert work_items == []
    assert len(prefilled) == len(items)
    assert all(result.status is MutantStatus.NO_COVERAGE for result in prefilled.values())
    assert all(result.covering == 0 for result in prefilled.values())


def test_module_level_mutants_all_reach_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "TIMEOUT = 0\nLIMIT = 5\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    items = [m for m in enumerate_mutants("src/x.py", source) if m.operator.startswith("lang:const:num:")]
    assert len(items) >= 2
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    coverage = FixedCoverage({"tests/x_test.py::test_x"})
    store = Store(tmp_path / "cache.sqlite")
    try:
        work_items, prefilled, _hits = partition.partition(
            items,
            {"src/x.py": source},
            coverage,
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert work_items == []
    assert len(prefilled) == len(items)
    assert all(result.status is MutantStatus.UNSUPPORTED for result in prefilled.values())


def test_site_key_uses_the_byte_offset_relative_to_the_function_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "import os\n\n\ndef valid(timeout: int) -> bool:\n    return timeout <= 0\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_valid():\n    assert True\n")
    item = next(
        m
        for m in enumerate_mutants("src/x.py", source)
        if m.operator.startswith("lang:const:num:") and m.original == "0"
    )
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = partition.partition(
            [item],
            {item.path: source},
            FixedCoverage({"tests/x_test.py::test_valid"}),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert len(items) == 1
    identity = partition.identify(item, source, module="x", identity_cache={})
    assert identity is not None
    expected = mutant_key(
        item.path,
        identity["qualname"],
        identity["func_src"],
        item.operator,
        item.mutated,
        item.start_byte - identity["func_start_byte"],
    )
    assert items[0].key == expected


def test_partition_without_store_never_reads_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    items, _prefilled, hits = partition.partition(
        [item],
        {item.path: source},
        FixedCoverage({"tests/x_test.py::test_valid"}),
        layout=make_layout(tmp_path),
        store=None,
        read_cache=True,
        memo={},
        reexport={},
    )
    assert hits == 0
    assert len(items) == 1


def test_cache_hit_updates_a_stale_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=False
        )
        cached = items[0]
        store.put_verdict(
            key=cached.key,
            file=item.path,
            lineno=item.lineno,
            operator=item.operator,
            original=item.original,
            mutated=item.mutated,
            qualname=cached.qualname,
            func_hash=cached.func_hash,
            verdict="survived",
            bucket="high",
            covering=list(cached.covering),
            covering_hash=cached.covering_hash,
        )
        rerun, _prefilled, hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=True
        )
        assert rerun == []
        assert hits == 1
        verdict = store.get_verdict(cached.key)
        assert verdict is not None
        assert verdict["bucket"] == "review"
    finally:
        store.close()


def test_persist_verdict_stores_a_kill_record_and_nothing_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    deps = (("src/x.py", "valid"),)
    try:
        items, _prefilled, _hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=False
        )
        result = MutantResult(item, MutantStatus.KILLED, 1, items[0].key, items[0].covering)
        partition.persist_verdict(store, items[0], result, [], kill=partition.KillRecord(deps, "validity"))
        recorded = store.get_verdict(items[0].key)
        partition.persist_verdict(store, items[0], result, [])
        unrecorded = store.get_verdict(items[0].key)
    finally:
        store.close()

    assert recorded is not None
    assert (recorded["kill_deps"], recorded["kill_validity"]) == (kill_deps_encode(deps), "validity")
    assert unrecorded is not None
    assert (unrecorded["kill_deps"], unrecorded["kill_validity"]) == ("", "")


def test_work_shares_the_module_text_and_only_its_request_spec_carries_the_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign holds each file once: every work item refers to the one module text, and the mutated copy exists
    only in the spec of a request."""
    source, _item = _setup_source(tmp_path, monkeypatch)
    mutants = [m for m in enumerate_mutants("src/x.py", source) if m.lineno == 2][:2]
    sources = {"src/x.py": source}

    items, _prefilled, _hits = partition.partition(
        mutants,
        sources,
        FixedCoverage({"tests/x_test.py::test_valid"}),
        layout=make_layout(tmp_path),
        store=None,
        read_cache=False,
        memo={},
        reexport={},
    )

    assert len(items) == 2
    assert all(work.source is sources["src/x.py"] for work in items)
    assert all("mutated_source" not in work.spec for work in items)
    specs = [work.request_spec() for work in items]
    assert [spec["mutated_source"] for spec in specs] == [apply_mutant(source, m) for m in mutants]
    assert [
        {**work.spec, "mutated_source": spec["mutated_source"]} for work, spec in zip(items, specs, strict=True)
    ] == specs


def test_partition_passes_path_then_lineno_to_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    seen: list[tuple[object, object]] = []

    class PathLinenoCoverage:
        def tests_covering(self, path: str, lineno: int, /) -> set[str]:
            seen.append((path, lineno))
            return {"tests/x_test.py::test_valid"}

        def files_covered_by(self, test_ids: set[str], /) -> set[str]:
            _ = test_ids
            return set()

    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = partition.partition(
            [item],
            {item.path: source},
            PathLinenoCoverage(),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert seen == [(item.path, item.lineno)]
    assert len(items) == 1


_NOT_PAREN_SOURCE = (
    "def next_version(part, prerelease):\n"
    "    if not (\n"
    '        part in ("major", "minor")\n'
    "        and prerelease is None\n"
    "    ):\n"
    "        return 1\n"
    "    return 2\n"
)


def _not_removal(path: str) -> Mutant:
    return next(m for m in enumerate_mutants(path, _NOT_PAREN_SOURCE) if m.operator == "lang:unary:not-removal")


def test_covering_tests_reads_every_line_of_a_multiline_edit() -> None:
    # CPython 3.14 compiles no instruction for the ``if not (`` line: its coverage is always empty.
    item = _not_removal("src/x.py")
    assert (item.lineno, item.site_end_lineno) == (2, 5)
    coverage = CoverageMap(
        {
            "src/x.py": {
                3: frozenset({"tests/x_test.py::a"}),
                4: frozenset({"tests/x_test.py::b"}),
                6: frozenset({"tests/x_test.py::after"}),  # the line after the edit
            }
        }
    )

    assert partition.covering_tests(coverage, item, _NOT_PAREN_SOURCE) == {"tests/x_test.py::a", "tests/x_test.py::b"}


def test_covering_tests_reads_a_folded_argument_on_the_line_of_its_call() -> None:
    # The constant arguments before ``*args`` are folded into one constant numbered with the call's first line: their
    # own lines are never recorded, the call's line is.
    source = (
        "def run(call, *args):\n"
        "    return call(\n"
        '        "-c",\n'
        '        "x=y",\n'
        '        "diff",\n'
        "        *args,\n"
        "    )\n"
    )
    item = next(
        m for m in enumerate_mutants("src/x.py", source) if m.operator == "lang:call:arg-drop" and m.lineno == 3
    )
    coverage = CoverageMap({"src/x.py": {2: frozenset({"tests/x_test.py::call"})}})

    assert partition.covering_tests(coverage, item, source) == {"tests/x_test.py::call"}


def test_covering_tests_selects_a_default_argument_mutant_by_its_body_line() -> None:
    source = "def f(\n    x=1,\n):\n    return x\n"
    item = next(m for m in enumerate_mutants("src/x.py", source) if m.default_index is not None)
    assert (item.site_lineno, item.lineno) == (2, 4)
    lines = {2: frozenset({"tests/x_test.py::importer"}), 4: frozenset({"tests/x_test.py::caller"})}

    assert partition.covering_tests(CoverageMap({"src/x.py": lines}), item, source) == {"tests/x_test.py::caller"}


def test_partition_runs_a_multiline_edit_whose_first_line_has_no_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "src/x.py", _NOT_PAREN_SOURCE)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    item = _not_removal("src/x.py")
    coverage = CoverageMap({"src/x.py": {3: frozenset({"tests/x_test.py::test_x"})}})

    items, prefilled, _hits = partition.partition(
        [item],
        {item.path: _NOT_PAREN_SOURCE},
        coverage,
        layout=make_layout(tmp_path),
        store=None,
        read_cache=False,
        memo={},
        reexport={},
    )

    assert prefilled == {}
    assert [work_item.covering for work_item in items] == [("tests/x_test.py::test_x",)]


def test_work_carries_func_hash_and_qualname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=False
        )
    finally:
        store.close()
    assert len(items) == 1
    identity = partition.identify(item, source, module="x", identity_cache={})
    assert identity is not None
    assert items[0].func_hash == sha(identity["func_src"])
    assert items[0].qualname == identity["qualname"]


def test_work_carries_the_fact_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "def f(a, b):\n    return a < b\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    item = next(m for m in enumerate_mutants("src/x.py", source) if m.operator.startswith("lang:compare"))
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = partition.partition(
            [item],
            {item.path: source},
            FixedCoverage({"tests/x_test.py::test_x"}),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert len(items) == 1
    assert items[0].bucket is Bucket.HIGH


def test_cache_hit_rebuilds_the_full_result_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    coverage = FixedCoverage({"tests/x_test.py::test_valid", "tests/x_test.py::test_other"})
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(tmp_path, item, source, store, coverage, read_cache=False)
        cached = items[0]
        store.put_verdict(
            key=cached.key,
            file=item.path,
            lineno=item.lineno,
            operator=item.operator,
            original=item.original,
            mutated=item.mutated,
            qualname=cached.qualname,
            func_hash=cached.func_hash,
            verdict="killed",
            bucket="review",
            covering=list(cached.covering),
            covering_hash=cached.covering_hash,
            killed_by=["tests/x_test.py::test_valid"],
        )
        rerun, prefilled, hits = _partition(tmp_path, item, source, store, coverage, read_cache=True)
    finally:
        store.close()
    assert rerun == []
    assert hits == 1
    result = prefilled[0]
    assert result.status is MutantStatus.KILLED
    assert result.covering == 2
    assert result.key == cached.key
    assert result.covering_tests == ("tests/x_test.py::test_other", "tests/x_test.py::test_valid")
    assert result.killed_by == ("tests/x_test.py::test_valid",)


def test_partition_forwards_custom_semantic_to_no_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    custom = (("zero", ("0",)),)
    try:
        _items, results, _hits = partition.partition(
            [item],
            {item.path: source},
            FixedCoverage(set()),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
            semantic=custom,
        )
        assert results[0].status is MutantStatus.NO_COVERAGE
        records = records_from_store(store, [item.path])
    finally:
        store.close()
    assert records[0]["semantic"] == ["zero"]


def test_persist_verdict_uses_custom_semantic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, item = _setup_source(tmp_path, monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = _partition(
            tmp_path, item, source, store, FixedCoverage({"tests/x_test.py::test_valid"}), read_cache=False
        )
        result = MutantResult(item, MutantStatus.SURVIVED, 1, items[0].key, items[0].covering)
        partition.persist_verdict(store, items[0], result, [], semantic=(("zero", ("0",)),))
        records = records_from_store(store, [item.path], {items[0].key})
    finally:
        store.close()
    assert records[0]["semantic"] == ["zero"]


def test_partition_hashes_covered_source_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "def valid(timeout: int) -> bool:\n    return timeout <= 0\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "src/helper.py", "def h():\n    return 2\n")
    write_file(tmp_path / "tests/x_test.py", "def test_valid():\n    assert True\n")
    item = next(
        m
        for m in enumerate_mutants("src/x.py", source)
        if m.operator.startswith("lang:const:num:") and m.original == "0"
    )
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)

    class HelperCoverage:
        def tests_covering(self, path: str, lineno: int, /) -> set[str]:
            _ = (path, lineno)
            return {"tests/x_test.py::test_valid"}

        def files_covered_by(self, test_ids: set[str], /) -> set[str]:
            _ = test_ids
            return {"src/helper.py"}

    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = partition.partition(
            [item],
            {item.path: source},
            HelperCoverage(),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
        )
    finally:
        store.close()
    assert len(items) == 1
    assert items[0].covering_hash


def test_partition_forwards_schedule_fuzz_to_identify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    src = "import asyncio\n\nasync def f(lock):\n    async with lock:\n        await asyncio.sleep(0)\n"
    write_file(tmp_path / "src/x.py", src)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    start = src.index("lock:")
    item = Mutant(
        "src/x.py",
        4,
        start,
        start + 4,
        "lock",
        "nullcontext()",
        "std:asyncio:lock-drop",
        fact=LockDrop(),
        family="concurrency",
    )
    store = Store(tmp_path / "cache.sqlite")
    try:
        items, _prefilled, _hits = partition.partition(
            [item],
            {item.path: src},
            FixedCoverage({"tests/x_test.py::test_x"}),
            layout=make_layout(tmp_path),
            store=store,
            read_cache=False,
            memo={},
            reexport={},
            schedule_fuzz=True,
        )
    finally:
        store.close()
    assert len(items) == 1
    assert "balamut_fuzz_yield" in items[0].request_spec()["mutated_source"]


@dataclass(frozen=True, slots=True)
class _FuzzedOracleFact:
    """A self-covered fact whose mutants also take schedule-fuzz yield points."""

    self_covered: ClassVar[bool] = True
    fuzz: ClassVar[bool] = True


def test_partition_forwards_schedule_fuzz_to_a_self_covered_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    source = "import asyncio\n\n\nasync def test_wave():\n    await asyncio.sleep(0)\n    assert 1 < 2\n"
    write_file(tmp_path / "tests/x_test.py", source)
    site = next(m for m in enumerate_mutants("tests/x_test.py", source) if m.operator.startswith("lang:compare"))
    oracle = Mutant(
        site.path,
        site.lineno,
        site.start_byte,
        site.end_byte,
        site.original,
        site.mutated,
        "lang:assert:negate",
        fact=_FuzzedOracleFact(),
        family="oracle",
    )

    items, _prefilled, _hits = partition.partition(
        [oracle],
        {oracle.path: source},
        FixedCoverage(set()),
        layout=make_layout(tmp_path),
        store=None,
        read_cache=False,
        memo={},
        reexport={},
        schedule_fuzz=True,
        collected_test_nodeids=frozenset({"tests/x_test.py::test_wave"}),
    )

    assert len(items) == 1
    assert "balamut_fuzz_yield" in items[0].request_spec()["mutated_source"]


def _record_function_source(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every function whose text ``identify`` resolves from the module source."""
    resolved: list[str] = []
    real = identify.function_source

    def recording(source: str, qualname: str, *, lineno: int | None = None) -> str | None:
        resolved.append(qualname)
        return real(source, qualname, lineno=lineno)

    monkeypatch.setattr(identify, "function_source", recording)
    return resolved


class _CountingCoverage:
    """One covering test for every site, counting the lookups of the files that test executes."""

    def __init__(self) -> None:
        self.file_lookups = 0

    def tests_covering(self, _path: str, _lineno: int, /) -> set[str]:
        return {"tests/x_test.py::test_x"}

    def files_covered_by(self, _test_ids: set[str], /) -> set[str]:
        self.file_lookups += 1
        return set()


def test_partition_resolves_a_function_and_a_covering_set_once_for_all_their_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "def f(a, b):\n    return (a + 1, b + 2)\n"
    write_file(tmp_path / "src/x.py", source)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    items = [m for m in enumerate_mutants("src/x.py", source) if m.operator.startswith("lang:const:num:")]
    assert len(items) >= 2
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    resolved = _record_function_source(monkeypatch)
    coverage = _CountingCoverage()

    work_items, _prefilled, _hits = partition.partition(
        items,
        {"src/x.py": source},
        coverage,
        layout=make_layout(tmp_path),
        store=None,
        read_cache=False,
        memo={},
        reexport={},
    )

    assert len(work_items) == len(items)
    assert resolved == ["f"]
    assert coverage.file_lookups == 1


def test_partition_resolves_an_oracle_test_function_once_for_all_its_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "def test_wave():\n    assert 1 < 2\n    assert 3 < 4\n"
    oracles = _oracles(tmp_path, monkeypatch, "tests/x_test.py", source, predicate="lang:compare")
    assert len(oracles) >= 2
    resolved = _record_function_source(monkeypatch)

    items, _prefilled, _hits = partition.partition(
        oracles,
        {oracles[0].path: source},
        FixedCoverage(set()),
        layout=make_layout(tmp_path),
        store=None,
        read_cache=False,
        memo={},
        reexport={},
        collected_test_nodeids=frozenset({"tests/x_test.py::test_wave"}),
    )

    assert len(items) == len(oracles)
    assert resolved == ["test_wave"]
