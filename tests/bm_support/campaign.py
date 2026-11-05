"""A scripted campaign runtime: daemons, requests and markers replaced by recorded fakes.

``stub_campaign_runtime`` lets ``run_campaign`` run its real verdict logic (paired originals, fresh confirmation,
retries, hang causality, kill dependencies) against a scripted sequence of request outcomes.
"""

from __future__ import annotations

import dataclasses
import queue
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from capability import Capability

from balamut.config import Layout
from balamut.core.mutants import Mutant
from balamut.coverage.store import CostMap, Store
from balamut.engine import campaign, daemon, partition, worker
from balamut.engine.daemon import Daemon, Hang, RunResult
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import MutantResult, MutantStatus
from balamut.isolation import EnvMap, EnvOverlays, Isolation, NoIsolation
from balamut.runtime.kill_deps import KILL_DEPS_SUFFIX, KillDeps, kill_deps_append
from balamut.runtime.plugin import TOUCHED_MARKER_SUFFIX, UNSUPPORTED_MARKER_SUFFIX
from bm_support import make_layout
from bm_support.daemons import fake_daemon

NO_KILLERS: frozenset[str] = frozenset()
SOURCE = {"src/x.py": "def f():\n    return 1\n"}

type Response = RunResult | Hang | None  # None: the request hangs with nothing reported


class FixedCoverage:
    """Every site is covered by ``covering`` (default: one test)."""

    def __init__(self, covering: set[str] | None = None) -> None:
        super().__init__()
        self._covering = covering if covering is not None else {"tests/x_test.py::test_x"}

    def tests_covering(self, _path: str, _lineno: int, /) -> set[str]:
        return self._covering

    def files_covered_by(self, _test_ids: set[str], /) -> set[str]:
        return set()


@dataclass(slots=True)
class FakeIsolation:
    """Isolated slots that only record their lifecycle."""

    isolated: bool = True
    prepared: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    cleaned: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])

    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        self.prepared.append((count, prefix))
        return tuple({"APP_DATABASE": f"{prefix}_w{index}"} for index in range(count))

    def cleanup(self, count: int, prefix: str, /) -> None:
        self.cleaned.append((count, prefix))

    def probe(self) -> None:
        return

    def fingerprint(self) -> str:
        return "fake-isolation"


def no_pool_action(_pool: list[Daemon]) -> None:
    return None


def no_missing_costs(_work: list[partition.Work], _costs: CostMap) -> list[str]:
    return []


def missing_costs(*nodeids: str) -> Callable[[list[partition.Work], CostMap], list[str]]:
    def missing(_work: list[partition.Work], _costs: CostMap) -> list[str]:
        return list(nodeids)

    return missing


def same_pool(calibration: list[Daemon], _work: list[partition.Work], _costs: CostMap) -> list[Daemon]:
    return calibration


def workers_count(count: int) -> Callable[[], int]:
    def workers() -> int:
        return count

    return workers


def fixed_support_hash(_layout: Layout, _memo: dict[str, str]) -> str:
    return "support"


def survive_one(
    _daemon: Daemon,
    pending: queue.Queue[partition.Work],
    results: partition.ResultsByIndex,
    progress: CampaignProgress,
    *_args: object,
    **_kwargs: object,
) -> None:
    """A worker loop that takes one mutant and declares it survived."""
    try:
        item = pending.get_nowait()
    except queue.Empty:
        return
    result = MutantResult(item.mutant, MutantStatus.SURVIVED, len(item.covering), item.key, item.covering)
    results[item.index] = result
    progress.advance("test", result.status, result.key)


def mutant(
    path: str, lineno: int, *, operator: str = "compare:Lt->LtE", start_byte: int = 0, fact: Capability | None = None
) -> Mutant:
    return Mutant(
        path=path,
        lineno=lineno,
        start_byte=start_byte,
        end_byte=start_byte + 1,
        original="a",
        mutated="b",
        operator=operator,
        fact=fact,
    )


def work(index: int, item: Mutant, covering: tuple[str, ...] = ("tests/x_test.py::test_x",)) -> partition.Work:
    return partition.Work(
        index=index,
        mutant=item,
        covering=covering,
        spec={"module": "x", "qualname": "f", "mutated_source": "def f():\n    return 2\n", "kind": "src"},
        key=f"key-{index}",
        func_hash="func-hash",
        qualname="f",
        covering_hash="covering-hash",
    )


def options(
    tmp_path: Path,
    *,
    layout: Layout | None = None,
    isolation: Isolation | None = None,
    daemons: int = 1,
    workers: int = 1,
    timeout_retries: int = 0,
    **overrides: float | bool | Path | None,
) -> campaign.CampaignOptions:
    base = campaign.CampaignOptions(
        address_base=str(tmp_path / "mutate.sock"),
        layout=layout or make_layout(tmp_path),
        scratch=tmp_path / "run",
        isolation=isolation or NoIsolation(),
        daemons=daemons,
        workers=workers,
        timeout_retries=timeout_retries,
        progress_mode=ProgressMode.NONE,
    )
    return dataclasses.replace(base, **overrides)


def run(
    tmp_path: Path,
    mutants: list[Mutant],
    *,
    store: Store | None = None,
    read_cache: bool = False,
    sources: dict[str, str] | None = None,
    initial_costs: dict[str, float] | None = None,
    opts: campaign.CampaignOptions | None = None,
) -> list[MutantResult]:
    return campaign.run_campaign(
        mutants,
        sources or SOURCE,
        FixedCoverage(),
        options=opts or options(tmp_path),
        store=store,
        read_cache=read_cache,
        initial_costs=initial_costs,
    )


@dataclass(slots=True)
class StubRecord:
    spawn_shapes: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    timeouts: list[float] = field(default_factory=list[float])
    persisted: list[MutantStatus] = field(default_factory=list[MutantStatus])
    requests: list[tuple[list[str], str]] = field(default_factory=list[tuple[list[str], str]])
    fresh: list[bool] = field(default_factory=list[bool])
    fresh_transitions: list[list[str]] = field(default_factory=list[list[str]])
    lifecycle: list[str] = field(default_factory=list[str])
    spawn_persistence: list[bool] = field(default_factory=list[bool])
    spawn_overlays: list[EnvOverlays] = field(default_factory=list[EnvOverlays])


def _fake_partition(
    work_items: list[partition.Work], prefilled: partition.ResultsByIndex | None, cache_hits: int
) -> Callable[..., tuple[list[partition.Work], partition.ResultsByIndex, int]]:
    def fake(*_args: object, **_kwargs: object) -> tuple[list[partition.Work], partition.ResultsByIndex, int]:
        return work_items, dict(prefilled or {}), cache_hits

    return fake


def _no_timing(*_args: float | object) -> None:
    return None


def _no_respawn(_daemon: Daemon) -> None:
    return None


def stub_campaign_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    work_items: list[partition.Work],
    responses: Sequence[Response],
    *,
    prefilled: partition.ResultsByIndex | None = None,
    cache_hits: int = 0,
    before_response: Callable[[], None] | None = None,
    runtime_unsupported: list[bool] | None = None,
    runtime_touched: list[bool] | None = None,
    touched_on_hang: bool = False,
    runtime_deps: list[KillDeps | None] | None = None,
    observe: Callable[[Daemon], None] | None = None,
) -> StubRecord:
    record = StubRecord()
    queue = list(responses)

    def fake_spawn_pool(
        address_base: str,
        daemons: int,
        workers: int,
        env: EnvMap,
        overlays: EnvOverlays,
        *,
        scratch: Path,
        persist_workers: bool = True,
        idle_ttl: int | None = None,
    ) -> list[Daemon]:
        _ = (scratch, idle_ttl)
        record.lifecycle.append(f"spawn:{address_base}")
        record.spawn_shapes.append((daemons, workers))
        record.spawn_persistence.append(persist_workers)
        record.spawn_overlays.append(overlays)
        spawned = fake_daemon(
            tmp_path, address=f"{address_base}.{len(record.spawn_shapes)}", index=len(record.spawn_shapes)
        )
        spawned.workers = workers
        spawned.env = {**env, **overlays[0]} if overlays else env
        spawned.persist_workers = persist_workers
        return [spawned]

    def fake_guarded(
        target: Daemon,
        nodeids: list[str],
        costs: daemon.CostMap,
        *,
        full: bool,
        timeout: float,
        label: str,
        detailed: bool = False,
        predicted_killers: AbstractSet[str] = NO_KILLERS,
        fresh_workers: bool = False,
    ) -> RunResult | Hang:
        _ = (full, detailed)
        record.lifecycle.append(f"run:{label}")
        record.requests.append((list(nodeids), target.mutant_file.read_text(encoding="utf-8")))
        record.fresh.append(fresh_workers)
        if observe is not None:
            observe(target)
        if before_response is not None:
            before_response()
        if runtime_unsupported is not None and runtime_unsupported.pop(0):
            Path(f"{target.mutant_file}{UNSUPPORTED_MARKER_SUFFIX}").write_text("unsupported", encoding="utf-8")
        record.timeouts.append(timeout)
        response = queue.pop(0)
        touched = runtime_touched.pop(0) if runtime_touched is not None else True
        hung = not isinstance(response, RunResult)
        # A hung request did not enter the mutated code object unless a test proves causality explicitly.
        if touched and (not hung or touched_on_hang):
            Path(f"{target.mutant_file}{TOUCHED_MARKER_SUFFIX}").write_text("touched", encoding="utf-8")
        recorded = runtime_deps.pop(0) if runtime_deps is not None else None
        if recorded is not None:
            # The daemon plugin writes the functions the lease entered next to its spec.
            kill_deps_append(Path(f"{target.mutant_file}{KILL_DEPS_SUFFIX}"), recorded)
        if response is None:
            return Hang(ordered=tuple(daemon.order_covering(nodeids, costs, predicted_killers)), completed=frozenset())
        return response

    original_persist = partition.persist_verdict

    def record_persist(
        store: Store, item: partition.Work, result: MutantResult, killed_by: list[str], **kwargs: object
    ) -> None:
        _ = kwargs
        record.persisted.append(result.status)
        original_persist(store, item, result, killed_by)

    def switch_to_fresh(pool: list[Daemon]) -> None:
        record.fresh_transitions.append([item.address for item in pool])

    def fake_adopt_pool(
        address_base: str, daemons: int, workers: int, env: EnvMap, scratch: Path, *, persist_workers: bool = True
    ) -> list[Daemon] | None:
        _ = (daemons, workers, env, scratch, persist_workers)
        record.lifecycle.append(f"adopt:{address_base}")
        return None

    monkeypatch.setattr(campaign, "resolve_workers", workers_count(10))
    monkeypatch.setattr(campaign, "partition", _fake_partition(work_items, prefilled, cache_hits))
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "adopt_pool", fake_adopt_pool)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn_pool)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "report_timing", _no_timing)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", switch_to_fresh)
    monkeypatch.setattr(worker, "respawn_daemon", _no_respawn)
    monkeypatch.setattr(worker, "run_subset_guarded", fake_guarded)
    monkeypatch.setattr(worker, "persist_verdict", record_persist)
    return record
