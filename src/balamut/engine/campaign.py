"""Run a mutation campaign: partition, lanes of daemons, timeout retries and fresh confirmation.

Ordinary mutants run on a pool of warm daemons; concurrency mutants run with one worker per daemon (in parallel
isolated lanes when the project has isolated slots, serially otherwise). Timeouts retry in isolation with an
escalated budget, and warm verdicts that never entered the mutated code are confirmed in a fresh fork per request.
Only terminal verdicts reach the cache; a timeout is always executed again by the next run.
"""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from operator import itemgetter
from pathlib import Path
from types import FrameType

from pytest_fast import resolve_workers

from balamut.config import DEFAULT_SEMANTIC, Layout
from balamut.core.mutants import Mutant
from balamut.core.semantic import SemanticPatterns
from balamut.coverage.discovery import ReexportIndex
from balamut.coverage.store import CostMap, Store
from balamut.diff.scope import ScopeWatch
from balamut.engine import planner
from balamut.engine.daemon import (
    Daemon,
    adopt_pool,
    await_pool,
    campaign_addopts,
    request,
    shutdown_pool,
    spawn_pool,
    switch_pool_to_fresh_workers,
)
from balamut.engine.partition import (
    CoverageLike,
    FileHashMemo,
    ResultsByIndex,
    SourceCache,
    Work,
    partition,
    persist_final,
    support_corpus_hash,
)
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import CampaignExactnessError, CampaignSetupError, MutantResult, MutantStatus, status_counts
from balamut.engine.stats import CampaignTimings, StatsPublisher, TimingView, empty_campaign_timings
from balamut.engine.timeouts import CostView, hang_wall_budget, timeout_policy_error
from balamut.engine.worker import Diagnostics, KillersByKey, MutantDiag, Timing, worker_loop
from balamut.isolation import EnvMap, EnvOverlays, Isolation, IsolationError, NoIsolation, slot_prefix
from balamut.operators.facts import LaneIsolated
from balamut.runtime.kill_deps import KillValidity

type DiagRow = dict[str, str | int | float]
type RequestsByOperator = dict[str, list[float]]
type _WorkByIndex = dict[int, Work]

_EMPTY: frozenset[str] = frozenset()
_COST_CALIBRATION_CHUNK_MAX = 64
_COST_CALIBRATION_CHUNKS_PER_DAEMON = 4
COST_CALIBRATION_FULL_SUITE_MIN = 1_000
_CONCURRENCY_MAX_DAEMONS = 4


def _no_isolation() -> Isolation:
    return NoIsolation()


@dataclass(frozen=True, slots=True)
class CampaignOptions:
    """Everything that shapes one campaign besides its mutants, coverage and cache."""

    address_base: str  # socket path prefix; every campaign appends a unique run suffix
    layout: Layout
    scratch: Path  # mutant specs and daemon logs
    isolation: Isolation = field(default_factory=_no_isolation)
    daemons: int = 0  # 0: decided by the planner after the cache partition
    workers: int = 1
    schedule_fuzz: bool = False
    cross_kill: bool = False
    mutant_timeout: float = 8.0
    timeout_retries: int = 1
    timeout_retry_multiplier: float = 3.0
    daemon_ttl: int | None = None  # None: the engine's built-in idle TTL
    persist_pool: bool = False  # keep the daemons at a fixed address for the next campaign instead of shutting down
    progress_mode: ProgressMode = ProgressMode.TTY
    stats_path: Path | None = None
    detailed: bool = False
    detailed_path: Path | None = None
    semantic: SemanticPatterns = DEFAULT_SEMANTIC


def missing_cost_nodeids(work: list[Work], costs: CostMap) -> list[str]:
    """Covering tests without a stored full cost, deterministic and without repeats."""
    return sorted({nodeid for item in work for nodeid in item.covering if nodeid not in costs})


def calibrate_costs(pool: list[Daemon], work: list[Work], costs: CostMap) -> list[Daemon]:
    """Measure the missing setup + call + teardown costs once, in parallel, on the original code.

    A cold store knows no test costs and would order tests by node id, paying for slow tests before a cheap killer.
    One wide daemon hands the whole covering set to pytest-fast's own scheduler; narrow pools share it in dynamic
    chunks without repeats. An empty mutant file means the original code. Returns the daemons used: they are
    disposable, since a baseline changes Python, C and database state no lease can roll back."""
    missing = missing_cost_nodeids(work, costs)
    if not missing:
        return []
    wide = len(pool) == 1 and pool[0].workers > 1
    full_suite = wide and len(missing) >= COST_CALIBRATION_FULL_SUITE_MIN
    if wide:
        chunks = [missing]
    else:
        target_chunks = max(1, len(pool) * _COST_CALIBRATION_CHUNKS_PER_DAEMON)
        size = min(_COST_CALIBRATION_CHUNK_MAX, max(1, math.ceil(len(missing) / target_chunks)))
        chunks = [missing[start : start + size] for start in range(0, len(missing), size)]
    calibrated = pool[: min(len(pool), len(chunks))]
    if not calibrated:
        raise RuntimeError("the baseline cost calibration got no daemon")
    pending: queue.Queue[list[str]] = queue.Queue()
    for chunk in chunks[len(calibrated) :]:
        pending.put(chunk)

    def measure(target: Daemon, first: list[str]) -> tuple[int, CostMap, list[str]]:
        measured: CostMap = {}
        chunk = first
        while True:
            failures: list[str] = []
            rc = request(target.address, None if full_suite else chunk, measured, failures, stop_on_failure=False)
            if rc != 0:
                return rc, measured, failures
            try:
                chunk = pending.get_nowait()
            except queue.Empty:
                return 0, measured, []

    shards: list[tuple[int, CostMap, list[str]]] = [(0, {}, [])] * len(calibrated)

    def run_shard(index: int) -> None:
        shards[index] = measure(calibrated[index], chunks[index])

    threads = [threading.Thread(target=run_shard, args=(index,)) for index in range(len(calibrated))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for rc, _measured, failures in shards:
        if rc != 0:
            raise RuntimeError(f"the baseline cost calibration is not green: {', '.join(failures[:3])}")
    for _rc, measured, _failures in shards:
        costs.update(measured)
    for nodeid in missing:
        costs.setdefault(nodeid, 0.0)  # an item without a report costs nothing
    return calibrated


def _calibration_pool(
    missing: int, pool_size: int, *, address_base: str, env: EnvMap, isolation: Isolation, scratch: Path
) -> tuple[list[Daemon], tuple[int, str] | None]:
    """A disposable pool for the baseline: one wide daemon on a shared environment; narrow daemons with slots of
    their own when the project is isolated (a shared database cannot take a parallel baseline)."""
    if not isolation.isolated:
        workers = max(1, min(resolve_workers(), missing))
        return spawn_pool(f"{address_base}.calibration", 1, workers, env, (), scratch=scratch), None
    count = max(1, min(pool_size, missing))
    prefix = slot_prefix()
    try:
        overlays = isolation.prepare(count, prefix)
    except IsolationError as exc:
        raise CampaignSetupError(f"could not prepare calibration slots: {exc}") from exc
    return spawn_pool(f"{address_base}.calibration", count, 1, env, overlays, scratch=scratch), (count, prefix)


def _cleanup_slots(isolation: Isolation, slots: tuple[int, str] | None) -> None:
    if slots is None:
        return
    try:
        isolation.cleanup(*slots)
    except IsolationError as exc:
        sys.stderr.write(f"warning: could not remove isolation slots {slots[1]}: {exc}\n")


def run_lane(
    work: list[Work],
    results: ResultsByIndex,
    progress: CampaignProgress,
    timing: Timing,
    costs: CostMap,
    store: Store | None,
    *,
    pool: list[Daemon],
    address_base: str,
    scratch: Path,
    cross_kill: bool,
    mutant_timeout: float,
    finalize_timeout: bool,
    isolation: Isolation | None = None,
    detailed: bool = False,
    diags: Diagnostics | None = None,
    killers_by_key: KillersByKey | None = None,
    defer_untouched: queue.Queue[Work] | None = None,
    reject_untouched: bool = False,
    reject_unstable_kill: bool = False,
    fresh_workers: bool = False,
    kill_validity: KillValidity | None = None,
    scope_watch: ScopeWatch | None = None,
    semantic: SemanticPatterns = DEFAULT_SEMANTIC,
) -> tuple[float, float]:
    """Execute one lane on ``pool`` (owned by the caller) -> (startup seconds, run seconds)."""
    if not work:
        return 0.0, 0.0
    isolation = isolation or NoIsolation()
    env = dict(os.environ)
    calibration: list[Daemon] = []
    calibration_slots: tuple[int, str] | None = None
    missing = missing_cost_nodeids(work, costs) if not cross_kill else []
    pending: queue.Queue[Work] = queue.Queue()
    fatal_errors: queue.Queue[CampaignExactnessError] = queue.Queue()
    for item in work:
        results.pop(item.index, None)
        pending.put(item)
    started = time.monotonic()
    try:
        if missing:
            calibration, calibration_slots = _calibration_pool(
                len(missing), len(pool), address_base=address_base, env=env, isolation=isolation, scratch=scratch
            )
        await_pool([*pool, *calibration])
        if calibration:
            calibrate_costs(calibration, work, costs)
            if store is not None:
                # A cold calibration is an expensive result of its own: published before the mutants run, so an
                # interrupted campaign does not pay for the baseline again.
                store.put_costs(costs)
            shutdown_pool(calibration)
            calibration = []
            _cleanup_slots(isolation, calibration_slots)
            calibration_slots = None
        startup_seconds = time.monotonic() - started
        run_started = time.monotonic()
        if not pool:
            raise RuntimeError("the mutation lane got no daemon")
        # A worker ends its thread after a transport failure and returns its item to the queue; other threads may
        # already have seen an empty queue. Bounded waves drain a late requeue; rotation keeps one daemon from
        # monopolizing a single remaining item.
        waves = 0
        for wave in range(len(pool) + 1):
            if pending.empty():
                break
            offset = wave % len(pool)
            threads = [
                threading.Thread(
                    target=worker_loop,
                    args=(target, pending, results, progress, timing, costs, store),
                    kwargs={
                        "cross_kill": cross_kill,
                        "mutant_timeout": mutant_timeout,
                        "finalize_timeout": finalize_timeout,
                        "detailed": detailed,
                        "diags": diags,
                        "killers_by_key": killers_by_key,
                        "defer_untouched": defer_untouched,
                        "reject_untouched": reject_untouched,
                        "reject_unstable_kill": reject_unstable_kill,
                        "fresh_workers": fresh_workers,
                        "fatal_errors": fatal_errors,
                        "kill_validity": kill_validity,
                        "scope_watch": scope_watch,
                        "semantic": semantic,
                    },
                )
                for target in [*pool[offset:], *pool[:offset]]
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if not fatal_errors.empty():
                raise fatal_errors.get_nowait()
            waves += 1
            if all(item.index in results for item in work):
                break
        run_seconds = time.monotonic() - run_started
        missing_verdicts = [item.index for item in work if item.index not in results]
        if missing_verdicts:
            raise RuntimeError(
                f"the mutation lane is incomplete: {len(missing_verdicts)} mutant(s) without a verdict "
                f"({pending.qsize()} queued) after {waves} drain waves. Run again: the cache keeps terminal verdicts."
            )
        return startup_seconds, run_seconds
    finally:
        if calibration:
            shutdown_pool(calibration)
        _cleanup_slots(isolation, calibration_slots)


def assert_campaign_complete(
    mutant_count: int, results: ResultsByIndex, progress: CampaignProgress, *, work_count: int, cache_hits: int
) -> None:
    """Exactly one terminal verdict per mutant, and no negative counter."""
    expected = set(range(mutant_count))
    if set(results) != expected:
        missing = sorted(expected - set(results))
        unexpected = sorted(set(results) - expected)
        raise RuntimeError(f"the campaign is incomplete: missing={missing}, unexpected={unexpected}")
    counters = Counter(result.status.value for result in results.values())
    if any(
        value < 0 for value in (mutant_count, work_count, cache_hits, progress.total, progress.done, *counters.values())
    ):
        raise RuntimeError("the campaign has a negative counter")
    if progress.total != work_count or progress.done != work_count:
        raise RuntimeError(
            f"the campaign progress is incomplete: done={progress.done}, total={progress.total}, work={work_count}"
        )
    if sum(counters.values()) != mutant_count:
        raise RuntimeError("the campaign does not hold exactly one terminal status per mutant")


def report_timing(prep: float, startup: float, run: float, timing: Timing, daemons: int) -> None:
    n = max(timing.n, 1)
    sys.stderr.write(
        "=== stage timing ===\n"
        f"  prep     (parse, apply, partition):         {prep:6.1f}s\n"
        f"  startup  ({daemons} daemons collecting in parallel): {startup:6.1f}s\n"
        f"  run      (parallel wall, {timing.n} requests):      {run:6.1f}s\n"
        f"    per mutant: spec write {1000 * timing.write / n:5.0f}ms"
        f" + request (fork, patch, tests, bus) {1000 * timing.request / n:5.0f}ms\n"
    )


def _diag_row(row: MutantDiag) -> DiagRow:
    overhead = max(0.0, row.request_s - (row.setup_s + row.call_s + row.teardown_s))
    return {
        "key": row.key,
        "path": row.path,
        "lineno": row.lineno,
        "operator": row.operator,
        "status": row.status,
        "covering": row.covering,
        "tests_run": row.tests_run,
        "setup_s": round(row.setup_s, 4),
        "call_s": round(row.call_s, 4),
        "teardown_s": round(row.teardown_s, 4),
        "overhead_s": round(overhead, 4),
        "request_s": round(row.request_s, 4),
        "write_s": round(row.write_s, 4),
        "killer": row.killer,
    }


def report_detailed(diags: Diagnostics, path: Path | None) -> None:
    """Where the time of the run went (``--detailed``): a measurement for finding speedups, never a gate.

    Per-mutant JSON goes to ``path``; a human breakdown goes to stderr. A broken path only warns."""
    rows = diags.rows
    if not rows:
        sys.stderr.write("=== --detailed: no mutant executed (all cached or uncovered) ===\n")
        return
    n = len(rows)
    write = sum(row.write_s for row in rows)
    setup = sum(row.setup_s for row in rows)
    call = sum(row.call_s for row in rows)
    teardown = sum(row.teardown_s for row in rows)
    overhead = sum(max(0.0, row.request_s - (row.setup_s + row.call_s + row.teardown_s)) for row in rows)
    grand = write + setup + call + teardown + overhead or 1.0
    tests_hist = Counter(row.tests_run for row in rows)
    mean_tests = sum(row.tests_run for row in rows) / n
    by_operator: RequestsByOperator = {}
    for row in rows:
        by_operator.setdefault(row.operator.partition(":")[0], []).append(row.request_s)
    if path is not None:
        try:
            path.write_text(
                json.dumps([_diag_row(row) for row in rows], ensure_ascii=False, indent=0), encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write(f"warning: --detailed-json was not written ({path}): {exc}\n")

    def share(seconds: float) -> str:
        return f"{1000 * seconds / n:6.0f}ms/mutant {100 * seconds / grand:4.0f}%"

    out = [
        "=== --detailed: cost breakdown of the mutation run ===",
        f"  mutants executed: {n}   sum of per-mutant wall: {grand:.1f}s   (JSON: {path or '-'})",
        "",
        "  where the time goes:",
        f"    spec write          {share(write)}",
        f"    fork + bus overhead {share(overhead)}",
        f"    fixture setup       {share(setup)}",
        f"    test call / db      {share(call)}",
        f"    teardown            {share(teardown)}",
        "",
        f"  tests per mutant until the first kill: mean {mean_tests:.1f}, max {max(row.tests_run for row in rows)}",
        "    histogram: " + ", ".join(f"{tests} tests x{count}" for tests, count in sorted(tests_hist.items())),
        "",
        "  mean request by operator class (by total time):",
    ]
    for operator, requests in sorted(by_operator.items(), key=lambda pair: -sum(pair[1])):
        out.append(
            f"    {operator:16s} {1000 * sum(requests) / len(requests):6.0f}ms/mutant"
            f"  (n={len(requests)}, total={sum(requests):.1f}s)"
        )
    dominant = max(
        [
            ("fork + bus overhead: persistent workers, more daemons", overhead),
            ("fixture setup: persistent workers, session-scoped fixtures", setup),
            ("test call / db: fewer tests per mutant, faster tests", call),
        ],
        key=itemgetter(1),
    )
    out += ["", f"  dominant: {dominant[0]} ({100 * dominant[1] / grand:.0f}% of the wall)"]
    sys.stderr.write("\n".join(out) + "\n")


def _covered_costs(covering: list[str], initial_costs: CostView | None) -> CostMap:
    if initial_costs is None:
        return {}
    return {
        nodeid: float(seconds)
        for nodeid in covering
        if (seconds := initial_costs.get(nodeid)) is not None and math.isfinite(seconds) and seconds >= 0
    }


def _drain_confirmations(untouched: queue.Queue[Work], results: ResultsByIndex) -> list[Work]:
    confirmation: _WorkByIndex = {}
    while True:
        try:
            item = untouched.get_nowait()
        except queue.Empty:
            return list(confirmation.values())
        result = results.get(item.index)
        if result is not None and result.status in {MutantStatus.KILLED, MutantStatus.SURVIVED}:
            confirmation[item.index] = item


def _split_concurrency(work: list[Work]) -> tuple[list[Work], list[Work]]:
    normal = [item for item in work if not isinstance(item.mutant.fact, LaneIsolated)]
    concurrency = [item for item in work if isinstance(item.mutant.fact, LaneIsolated)]
    return normal, concurrency


def run_campaign(
    mutants: list[Mutant],
    sources: SourceCache,
    cov: CoverageLike,
    *,
    options: CampaignOptions,
    store: Store | None,
    read_cache: bool,
    reexport: ReexportIndex | None = None,
    collected_test_nodeids: frozenset[str] = _EMPTY,
    phase_timings: TimingView | None = None,
    initial_costs: CostView | None = None,
    scope_watch: ScopeWatch | None = None,
) -> list[MutantResult]:
    """Execute the campaign -> one terminal result per mutant, in mutant order."""
    if (
        error := timeout_policy_error(options.mutant_timeout, options.timeout_retry_multiplier, options.timeout_retries)
    ) is not None:
        raise ValueError(error)
    layout = options.layout
    isolation = options.isolation
    # A unique run suffix: two concurrent campaigns on one address base never kill each other's daemons. A persisted
    # pool keeps a fixed address instead, so the next campaign adopts it; concurrent persisted campaigns would
    # collide on purpose.
    if options.persist_pool and isolation.isolated:
        raise CampaignSetupError("--persist-pool needs a shared (non-isolated) environment")
    address_base = (
        f"{options.address_base}.pool" if options.persist_pool else f"{options.address_base}.run-{uuid.uuid4().hex[:8]}"
    )
    t0 = time.monotonic()
    timings: CampaignTimings = empty_campaign_timings()
    if phase_timings is not None:
        timings.update({name: float(seconds) for name, seconds in phase_timings.items()})
    memo: FileHashMemo = {}
    # The kill-record boundary: a file changed later may have been executed by a daemon in its earlier version.
    files_read_at = time.time()
    partition_started = time.perf_counter()
    # One validity per campaign: the cache check and new kills share one parse of the tree and one support hash.
    validity = KillValidity(layout, support_hash=support_corpus_hash(layout, memo), since=files_read_at)
    validity.warm_layout()  # the parses the cached-verdict checks need, up front and in parallel
    work, results, cache_hits = partition(
        mutants,
        sources,
        cov,
        layout=layout,
        store=store,
        read_cache=read_cache,
        memo=memo,
        schedule_fuzz=options.schedule_fuzz,
        reexport=reexport or {},
        collected_test_nodeids=collected_test_nodeids,
        kill_validity=validity,
        semantic=options.semantic,
    )
    partition_seconds = time.perf_counter() - partition_started
    kill_validity = validity if store is not None else None
    plan = planner.resolve_execution_plan(
        explicit_daemons=options.daemons,
        workers_per_daemon=options.workers,
        pending=len(work),
        perf_cores=resolve_workers(),
        isolated=isolation.isolated,
    )
    daemons, workers = plan.daemons, plan.workers_per_daemon
    requested = str(options.daemons) if options.daemons > 0 else "auto"
    sys.stderr.write(
        f"parallelism: daemons {requested} -> {daemons} x {workers} workers = {plan.capacity}"
        f" ({len(work)} uncached mutants, {'isolated' if isolation.isolated else 'shared'} environment)\n"
    )
    covering_all = sorted({nodeid for item in work for nodeid in item.covering})
    costs: CostMap = store.get_costs(covering_all) if store is not None else {}
    fresh_costs = _covered_costs(covering_all, initial_costs)
    if fresh_costs:
        # The sidecar of the current substrate beats the historical cost store: a changed test must change the order
        # and the watchdog budget at once.
        costs.update(fresh_costs)
        if store is not None:
            store.put_costs(fresh_costs)
        sys.stderr.write(f"baseline costs: {len(fresh_costs)}/{len(covering_all)} from the coverage run\n")
    # The work weight of a mutant is the sum of its covering durations; an unknown one weighs the mean of the known
    # (an empty store weighs 1.0, so the weight degrades to the covering count).
    mean_cost = (sum(costs.values()) / len(costs)) if costs else 1.0
    weights: CostMap = {item.key: sum(costs.get(test, mean_cost) for test in item.covering) for item in work}
    publisher: StatsPublisher | None = None
    if options.stats_path is not None:
        publisher = StatsPublisher(
            path=options.stats_path,
            enumerated=len(mutants),
            cache_resolved=cache_hits,
            executed=len(work),
            status_counts=status_counts(results.values()),
            weights=weights,
            total_cost=sum(weights.values()),
            hang_weight=hang_wall_budget(
                options.mutant_timeout, options.timeout_retry_multiplier, options.timeout_retries
            ),
            timings=dict(timings),
        )
        publisher.publish_initial()
    t_prep = time.monotonic() - t0
    sys.stderr.write(f"to run: {len(work)}\n")
    timing = Timing()
    progress = CampaignProgress(
        len(work),
        options.progress_mode,
        on_advance=publisher.record_terminal if publisher is not None else None,
        on_hang=publisher.mark_hanging if publisher is not None else None,
    )
    diags = Diagnostics() if options.detailed else None
    # Kill prediction: a mutant's earlier killers go first, so a rerun usually dies on its first test. Verdicts do
    # not depend on it (killed iff some test fails).
    killers_by_key: KillersByKey | None = (
        {key: frozenset(killers) for key, killers in store.killed_with_killers()} if store is not None else None
    )
    t_startup = 0.0
    t_run = 0.0
    if work:
        t_startup, t_run = _execute(
            work,
            results,
            progress,
            timing,
            costs,
            store,
            options=options,
            address_base=address_base,
            daemons=daemons,
            workers=workers,
            covering_all=covering_all,
            diags=diags,
            killers_by_key=killers_by_key,
            kill_validity=kill_validity,
            scope_watch=scope_watch,
        )
    timings["cache_partition"] += partition_seconds
    timings["daemon_startup"] += t_startup
    timings["mutant_execution"] += t_run
    assert_campaign_complete(len(mutants), results, progress, work_count=len(work), cache_hits=cache_hits)
    if work:
        report_timing(t_prep, t_startup, t_run, timing, daemons)
    if diags is not None:
        report_detailed(diags, options.detailed_path)
    persistence_started = time.perf_counter()
    if store is not None:
        store.flush()
        persist_final(store, sources, {**costs, **timing.costs}, layout=layout)
    timings["persistence"] += time.perf_counter() - persistence_started
    if publisher is not None:
        publisher.publish_complete(timings)
    sys.stderr.write(f"cache: {cache_hits} hits, {len(work)} executed\n")
    return [results[index] for index in range(len(mutants))]


def _execute(
    work: list[Work],
    results: ResultsByIndex,
    progress: CampaignProgress,
    timing: Timing,
    costs: CostMap,
    store: Store | None,
    *,
    options: CampaignOptions,
    address_base: str,
    daemons: int,
    workers: int,
    covering_all: list[str],
    diags: Diagnostics | None,
    killers_by_key: KillersByKey | None,
    kill_validity: KillValidity | None,
    scope_watch: ScopeWatch | None,
) -> tuple[float, float]:
    """The daemon part of a campaign: owned slots, pools, lanes, retries and confirmation."""
    isolation = options.isolation

    def on_sigterm(_signum: int, _frame: FrameType | None) -> None:
        # The default SIGTERM ends the process past every finally: orphaned daemons. SystemExit runs the teardown.
        raise SystemExit(143)

    on_main_thread = threading.current_thread() is threading.main_thread()
    previous_sigterm = signal.signal(signal.SIGTERM, on_sigterm) if on_main_thread else signal.SIG_DFL
    normal_pool: list[Daemon] = []
    concurrency_pool: list[Daemon] = []
    slots: tuple[int, str] | None = None
    # Daemons and the client see the same addopts: the pools (spawned from os.environ) and the pytest-fast requests
    # both read them from the process environment, which is restored after the campaign.
    previous_addopts = os.environ.get("PYTEST_ADDOPTS")
    os.environ["PYTEST_ADDOPTS"] = campaign_addopts(
        previous_addopts or "", covering_all, hang_dump_after=options.mutant_timeout
    )
    t_startup = 0.0
    t_run = 0.0
    untouched: queue.Queue[Work] = queue.Queue()
    scratch = options.scratch
    lane = partial(
        run_lane,
        address_base=address_base,
        scratch=scratch,
        isolation=isolation,
        detailed=options.detailed,
        diags=diags,
        killers_by_key=killers_by_key,
        kill_validity=kill_validity,
        scope_watch=scope_watch,
        semantic=options.semantic,
    )
    try:
        overlays: EnvOverlays = ()
        if isolation.isolated:
            prefix = slot_prefix()
            try:
                overlays = isolation.prepare(daemons, prefix)
            except IsolationError as exc:
                raise CampaignSetupError(f"could not prepare isolation slots: {exc}") from exc
            slots = (daemons, prefix)
        normal_work, concurrency_work = _split_concurrency(work)
        concurrency_daemons = (
            min(_CONCURRENCY_MAX_DAEMONS, daemons, len(concurrency_work))
            if isolation.isolated and concurrency_work
            else 1
        )
        env = dict(os.environ)
        if normal_work:
            if options.persist_pool:
                normal_pool = adopt_pool(address_base, daemons, workers, env, scratch=scratch) or spawn_pool(
                    address_base, daemons, workers, env, overlays, scratch=scratch, idle_ttl=options.daemon_ttl
                )
            else:
                normal_pool = spawn_pool(
                    address_base, daemons, workers, env, overlays, scratch=scratch, idle_ttl=options.daemon_ttl
                )
        startup, runtime = lane(
            normal_work,
            results,
            progress,
            timing,
            costs,
            store,
            pool=normal_pool,
            cross_kill=options.cross_kill,
            mutant_timeout=options.mutant_timeout,
            finalize_timeout=options.timeout_retries == 0,
            defer_untouched=untouched,
        )
        t_startup += startup
        t_run += runtime
        # A full normal lane can outlast the idle TTL: the concurrency pool starts right before its own lane.
        if concurrency_work:
            concurrency_pool = spawn_pool(
                f"{address_base}.concurrency",
                concurrency_daemons,
                1,
                env,
                overlays[:concurrency_daemons],
                scratch=scratch,
                idle_ttl=options.daemon_ttl,
            )
        startup, runtime = lane(
            concurrency_work,
            results,
            progress,
            timing,
            costs,
            store,
            pool=concurrency_pool,
            cross_kill=options.cross_kill,
            mutant_timeout=options.mutant_timeout,
            finalize_timeout=options.timeout_retries == 0,
            defer_untouched=untouched,
        )
        t_startup += startup
        t_run += runtime

        by_index = {item.index: item for item in work}
        timed_out = [item for item in work if results[item.index].status is MutantStatus.TIMEOUT]
        fresh_mode = False
        if (timed_out and options.timeout_retries > 0) or not untouched.empty():
            switch_pool_to_fresh_workers([*normal_pool, *concurrency_pool])
            fresh_mode = True
        for retry in range(1, options.timeout_retries + 1):
            if not timed_out:
                break
            normal_timed_out, concurrency_timed_out = _split_concurrency(timed_out)
            groups = (
                (normal_timed_out, min(daemons, len(normal_timed_out)) if isolation.isolated else 1, normal_pool),
                (
                    concurrency_timed_out,
                    min(concurrency_daemons, len(concurrency_timed_out)) if concurrency_timed_out else 1,
                    concurrency_pool,
                ),
            )
            for retry_work, retry_daemons, retry_pool in groups:
                startup, runtime = lane(
                    retry_work,
                    results,
                    progress,
                    timing,
                    costs,
                    store,
                    pool=retry_pool[:retry_daemons],
                    cross_kill=options.cross_kill,
                    mutant_timeout=options.mutant_timeout * (options.timeout_retry_multiplier**retry),
                    finalize_timeout=retry == options.timeout_retries,
                    defer_untouched=untouched,
                    fresh_workers=True,
                )
                t_startup += startup
                t_run += runtime
            timed_out = [
                by_index[index]
                for index, result in results.items()
                if index in by_index and result.status is MutantStatus.TIMEOUT
            ]

        confirmation = _drain_confirmations(untouched, results)
        if confirmation:
            if not fresh_mode:
                switch_pool_to_fresh_workers([*normal_pool, *concurrency_pool])
            sys.stderr.write(
                f"\n  fresh confirmation: {len(confirmation)} mutant(s) with an unproven warm verdict;"
                " fresh slots and a fork per request\n"
            )
            if slots is not None:
                # A mutant may break commit, rollback or cleanup and leave data behind: confirmation starts clean.
                try:
                    isolation.prepare(*slots)
                except IsolationError as exc:
                    raise CampaignSetupError(f"could not refresh isolation slots for confirmation: {exc}") from exc
            normal_confirmation, concurrency_confirmation = _split_concurrency(confirmation)
            confirmation_timeout = options.mutant_timeout * (options.timeout_retry_multiplier**options.timeout_retries)
            for confirmation_work, confirmation_pool, limit in (
                (normal_confirmation, normal_pool, daemons),
                (concurrency_confirmation, concurrency_pool, concurrency_daemons),
            ):
                count = min(limit, len(confirmation_work)) if isolation.isolated and confirmation_work else 1
                startup, runtime = lane(
                    confirmation_work,
                    results,
                    progress,
                    timing,
                    costs,
                    store,
                    pool=confirmation_pool[:count],
                    cross_kill=True,
                    mutant_timeout=confirmation_timeout,
                    finalize_timeout=True,
                    reject_untouched=True,
                    reject_unstable_kill=True,
                    fresh_workers=True,
                )
                t_startup += startup
                t_run += runtime
    finally:
        shutdown_pool(concurrency_pool)
        if not options.persist_pool:
            shutdown_pool(normal_pool)
        if previous_addopts is None:
            os.environ.pop("PYTEST_ADDOPTS", None)
        else:
            os.environ["PYTEST_ADDOPTS"] = previous_addopts
        _cleanup_slots(isolation, slots)
        if on_main_thread:
            signal.signal(signal.SIGTERM, previous_sigterm)
        sys.stderr.write("\n")
    return t_startup, t_run
