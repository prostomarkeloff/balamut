"""Mutation-sensitive unit tests for balamut.engine.campaign survivors.

Covers the lanes, planner bookkeeping, cost calibration, timing/diagnostic reporting and the daemon
lifecycle helpers that the scripted-campaign tests in ``test_campaign.py`` exercise but do not assert on
tightly enough to kill every mutant.
"""

from __future__ import annotations

import importlib
import os
import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

import pytest

from balamut.core.mutants import Mutant
from balamut.coverage.store import Store
from balamut.engine import campaign, partition
from balamut.engine.daemon import Daemon, RunResult
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import MutantResult, MutantStatus
from balamut.engine.worker import Diagnostics, MutantDiag, Timing
from balamut.isolation import EnvMap, EnvOverlays, Isolation, IsolationError, NoIsolation
from balamut.operators.concurrency import LockDrop
from bm_support.campaign import (
    FakeIsolation,
    mutant,
    no_missing_costs,
    no_pool_action,
    options,
    run,
    stub_campaign_runtime,
    work,
    workers_count,
)
from bm_support.daemons import fake_daemon


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
    costs: dict[str, float] | None = None,
) -> tuple[float, float]:
    return campaign.run_lane(
        items,
        {} if results is None else results,
        CampaignProgress(len(items), ProgressMode.NONE),
        Timing(),
        {} if costs is None else costs,
        None,
        pool=pool,
        address_base=str(tmp_path / "mutate.sock"),
        scratch=tmp_path / "run",
        cross_kill=False,
        mutant_timeout=8.0,
        finalize_timeout=True,
        isolation=isolation,
    )


def _diag(
    key: str,
    operator: str,
    tests_run: int,
    *,
    write: float = 1.0,
    setup: float = 2.0,
    call: float = 3.0,
    teardown: float = 4.0,
    request: float = 20.0,
) -> MutantDiag:
    return MutantDiag(
        key=key,
        path="src/x.py",
        lineno=1,
        operator=operator,
        status="killed",
        covering=1,
        tests_run=tests_run,
        setup_s=setup,
        call_s=call,
        teardown_s=teardown,
        request_s=request,
        write_s=write,
        killer="",
    )


# --------------------------------------------------------------------------- module / class level (cold)
def test_module_constants_and_schema_defaults() -> None:
    """Re-import the module inside a test so the cold (module/class-level) lines get a covering test."""
    importlib.reload(campaign)
    assert campaign._COST_CALIBRATION_CHUNK_MAX == 64
    assert campaign._COST_CALIBRATION_CHUNKS_PER_DAEMON == 4
    assert campaign.COST_CALIBRATION_FULL_SUITE_MIN == 1_000
    assert campaign._CONCURRENCY_MAX_DAEMONS == 4
    fields = campaign.CampaignOptions.__dataclass_fields__
    assert fields["daemons"].default == 0
    assert fields["workers"].default == 1
    assert fields["schedule_fuzz"].default is False
    assert fields["cross_kill"].default is False
    assert fields["mutant_timeout"].default == 8.0
    assert fields["timeout_retries"].default == 1
    assert fields["timeout_retry_multiplier"].default == 3.0
    assert fields["detailed"].default is False
    # Type aliases evaluate lazily: force the value so a BitOr->BitAnd mutant raises.
    assert campaign.DiagRow.__value__ == dict[str, str | int | float]


def test_no_isolation_factory_returns_noisolation() -> None:
    assert isinstance(campaign._no_isolation(), NoIsolation)


# --------------------------------------------------------------------------- calibrate_costs
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


def test_calibrate_costs_shards_on_a_single_narrow_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single daemon with one worker is narrow: a big missing set must still shard, not become wide."""
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(200))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert len(calls) > 1  # narrow: sharded, not a single wide request


def test_calibrate_costs_wide_daemon_with_two_workers_is_wide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    campaign.calibrate_costs([fake_daemon(tmp_path, workers=2)], [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert calls == [("sock", sorted(nodeids), False)]


def test_calibrate_costs_caps_chunk_size_at_the_calibration_chunk_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``size`` is the min of the chunk max and the dynamic share; ``max->min`` would raise the cap to 64."""
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(200))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert max(len(shard or []) for _address, shard, _stop in calls) == 50


def test_calibrate_costs_floor_of_one_survives_a_tiny_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``max(1, ceil(n / target))`` floors the shard size at 1; ``max->min`` collapses every shard to one test."""
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert max(len(shard or []) for _address, shard, _stop in calls) == 2


def test_calibrate_costs_max_target_chunks_guards_a_zero_chunks_per_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``max(1, ...)`` around ``target_chunks`` only differs from ``max(2, ...)`` when chunks-per-daemon is 0."""
    monkeypatch.setattr(campaign, "_COST_CALIBRATION_CHUNKS_PER_DAEMON", 0)
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]
    campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert len(calls) == 1  # target_chunks==1 -> one shard; the mutation would produce two


def test_calibrate_costs_floor_of_one_keeps_a_single_test_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max(1, ceil(n / target))`` keeps the shard size at 1 when the share is exactly one; ``1->2`` doubles it."""
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(12))
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], {})
    # target_chunks = 3 * 4 = 12; ceil(12 / 12) == 1 -> size 1, not max(2, 1) == 2
    assert max(len(shard or []) for _address, shard, _stop in calls) == 1


def test_calibrate_costs_ignores_a_crashed_shard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shard whose request raises keeps its optimistic ``(0, {}, [])`` placeholder; ``0->1`` fails the campaign."""
    nodeids = ("tests/x_test.py::test_x",)

    def crash(*_args: object, **_kwargs: object) -> int:
        raise OSError("socket down")

    monkeypatch.setattr(campaign, "request", crash)
    costs: dict[str, float] = {}
    campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10), nodeids)], costs)
    assert costs == {"tests/x_test.py::test_x": 0.0}


def test_calibrate_costs_reports_first_three_failures_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A red baseline names at most three failing tests in the error message."""

    def red(
        _address: str, _nodeids: list[str] | None, _durations: dict[str, float], failures: list[str], **_kw: bool
    ) -> int:
        failures.extend(
            ["tests/x_test.py::test_a", "tests/x_test.py::test_b", "tests/x_test.py::test_c", "tests/x_test.py::test_d"]
        )
        return 1

    monkeypatch.setattr(campaign, "request", red)
    with pytest.raises(RuntimeError, match="baseline cost calibration is not green") as excinfo:
        campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10))], {})
    message = str(excinfo.value)
    assert "test_a" in message
    assert "test_b" in message
    assert "test_c" in message
    assert "test_d" not in message


def test_calibrate_costs_records_an_unreported_test_as_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A covering test without a report still lands in the cost map at 0.0."""
    nodeids = ("tests/x_test.py::test_reported", "tests/x_test.py::test_unreported")

    def request_(
        _address: str,
        nodeids_: list[str] | None,
        durations: dict[str, float],
        _failures: list[str],
        **_kw: bool,
    ) -> int:
        for nodeid in nodeids_ or []:
            if nodeid == "tests/x_test.py::test_reported":
                durations[nodeid] = 0.25
        return 0

    monkeypatch.setattr(campaign, "request", request_)
    costs: dict[str, float] = {}
    campaign.calibrate_costs([fake_daemon(tmp_path)], [work(0, mutant("src/x.py", 10), nodeids)], costs)
    assert costs == {"tests/x_test.py::test_reported": 0.25, "tests/x_test.py::test_unreported": 0.0}


def test_calibrate_costs_joins_every_shard_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every shard thread is joined before the failures are inspected."""
    nodeids = tuple(f"tests/x_test.py::test_{index}" for index in range(20))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    joined: list[str] = []

    def fake_request(
        address: str, _nodeids: list[str] | None, durations: dict[str, float], _failures: list[str], **_kw: bool
    ) -> int:
        durations.update(dict.fromkeys(nodeids, 0.25))
        return 0

    monkeypatch.setattr(campaign, "request", fake_request)
    real_join = threading.Thread.join

    def spy_join(self: threading.Thread, timeout: float | None = None) -> None:
        joined.append(self.name)
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", spy_join)
    campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert len(joined) == len(pool)


def test_calibrate_costs_slices_pool_to_chunk_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``pool[: min(len(pool), len(chunks))]`` returns one daemon per chunk; ``min->max`` would over-slice the pool."""
    nodeids = ("tests/x_test.py::test_x",)  # one missing -> one chunk
    calls: list[_RequestCall] = []
    monkeypatch.setattr(campaign, "request", _calibration_request(calls))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(3)]
    calibrated = campaign.calibrate_costs(pool, [work(0, mutant("src/x.py", 10), nodeids)], {})
    assert len(calibrated) == 1
    assert len(calls) == 1


# --------------------------------------------------------------------------- _calibration_pool
def test_calibration_pool_wide_daemon_bounds_workers_by_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared environment uses one wide daemon whose workers never exceed the missing count."""
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(8))
    spawned: list[tuple[int, int, EnvOverlays, Path]] = []

    def fake_spawn(
        address_base: str,
        daemons: int,
        workers: int,
        env: EnvMap,
        overlays: EnvOverlays,
        *,
        scratch: Path,
        **_kw: object,
    ) -> list[Daemon]:
        _ = (env, address_base)
        spawned.append((daemons, workers, overlays, scratch))
        return [fake_daemon(tmp_path, address=address_base)]

    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    pool, slots = campaign._calibration_pool(
        2, 4, address_base="sock", env={}, isolation=NoIsolation(), scratch=tmp_path / "run"
    )
    assert slots is None
    assert spawned == [(1, 2, (), tmp_path / "run")]
    assert [d.address for d in pool] == ["sock.calibration"]


def test_calibration_pool_floors_workers_at_one_when_resolve_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max(1, min(resolve_workers(), missing))`` floors the wide daemon at one worker; ``1->2`` over-spawns."""
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(1))
    spawned: list[tuple[int, int, EnvOverlays]] = []

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, address_base)
        spawned.append((daemons, workers, overlays))
        return [fake_daemon(tmp_path, address=address_base)]

    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    campaign._calibration_pool(5, 4, address_base="sock", env={}, isolation=NoIsolation(), scratch=tmp_path / "run")
    assert spawned == [(1, 1, ())]


def test_calibration_pool_isolated_never_exceeds_pool_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")
    spawned: list[tuple[int, int, EnvOverlays, Path]] = []
    isolation = FakeIsolation()

    def fake_spawn(
        address_base: str,
        daemons: int,
        workers: int,
        env: EnvMap,
        overlays: EnvOverlays,
        *,
        scratch: Path,
        **_kw: object,
    ) -> list[Daemon]:
        _ = (env, address_base)
        spawned.append((daemons, workers, overlays, scratch))
        return [fake_daemon(tmp_path, address=address_base) for _ in range(daemons)]

    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    pool, slots = campaign._calibration_pool(
        10, 3, address_base="sock", env={}, isolation=isolation, scratch=tmp_path / "run"
    )
    assert slots == (3, "prefix")
    assert spawned == [(3, 1, tuple({"APP_DATABASE": f"prefix_w{i}"} for i in range(3)), tmp_path / "run")]
    assert len(pool) == 3


# --------------------------------------------------------------------------- _cleanup_slots
def test_cleanup_slots_warns_and_swallows_a_failed_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingCleanup(FakeIsolation):
        def cleanup(self, count: int, prefix: str, /) -> None:
            raise IsolationError("boom")

    campaign._cleanup_slots(FailingCleanup(), (2, "prefix"))
    assert "could not remove isolation slots prefix" in capsys.readouterr().err


# --------------------------------------------------------------------------- assert_campaign_complete
def _result(mutant_: Mutant, status: MutantStatus) -> MutantResult:
    return MutantResult(mutant_, status, 1, "", ())


def test_assert_campaign_complete_reports_the_missing_mutant() -> None:
    item = mutant("src/x.py", 10)
    progress = CampaignProgress(1, ProgressMode.NONE)
    with pytest.raises(RuntimeError, match=r"missing=\[1\]"):
        campaign.assert_campaign_complete(
            2, {0: _result(item, MutantStatus.SURVIVED)}, progress, work_count=1, cache_hits=0
        )


def test_assert_campaign_complete_reports_the_unexpected_mutant() -> None:
    item = mutant("src/x.py", 10)
    progress = CampaignProgress(1, ProgressMode.NONE)
    with pytest.raises(RuntimeError, match=r"unexpected=\[2\]"):
        campaign.assert_campaign_complete(
            2,
            {
                0: _result(item, MutantStatus.SURVIVED),
                1: _result(item, MutantStatus.KILLED),
                2: _result(item, MutantStatus.KILLED),
            },
            progress,
            work_count=2,
            cache_hits=0,
        )


def test_assert_campaign_complete_rejects_a_single_negative_counter() -> None:
    item = mutant("src/x.py", 10)
    progress = CampaignProgress(2, ProgressMode.NONE)
    with pytest.raises(RuntimeError, match="negative counter"):
        campaign.assert_campaign_complete(
            2,
            {0: _result(item, MutantStatus.SURVIVED), 1: _result(item, MutantStatus.KILLED)},
            progress,
            work_count=-1,
            cache_hits=0,
        )


def test_assert_campaign_complete_rejects_partial_progress() -> None:
    item = mutant("src/x.py", 10)
    progress = CampaignProgress(2, ProgressMode.NONE)
    progress.done = 1
    with pytest.raises(RuntimeError, match="progress is incomplete"):
        campaign.assert_campaign_complete(
            2,
            {0: _result(item, MutantStatus.SURVIVED), 1: _result(item, MutantStatus.KILLED)},
            progress,
            work_count=2,
            cache_hits=0,
        )


# --------------------------------------------------------------------------- report_timing
def test_report_timing_prints_stage_timing(capsys: pytest.CaptureFixture[str]) -> None:
    campaign.report_timing(1.0, 2.0, 3.0, Timing(write=1.0, request=2.0, n=1), daemons=2)
    out = capsys.readouterr().err
    assert "=== stage timing ===" in out
    assert "1000ms" in out  # write 1.0 over n=1
    assert "2000ms" in out  # request 2.0 over n=1


def test_report_timing_guards_zero_requests(capsys: pytest.CaptureFixture[str]) -> None:
    campaign.report_timing(0.0, 0.0, 0.0, Timing(n=0), daemons=1)
    assert "=== stage timing ===" in capsys.readouterr().err


def test_report_timing_per_mutant_ms(capsys: pytest.CaptureFixture[str]) -> None:
    campaign.report_timing(0.0, 0.0, 0.0, Timing(write=0.004, request=0.006, n=2), daemons=1)
    out = capsys.readouterr().err
    # write: 1000*0.004/2 = 2.0 ; request: 1000*0.006/2 = 3.0
    assert "2ms" in out
    assert "3ms" in out


# --------------------------------------------------------------------------- _diag_row
def test_diag_row_overhead_arithmetic() -> None:
    row = _diag("k", "lang:binop", 2, write=0.5, setup=1.0, call=2.0, teardown=3.0, request=6.5)
    assert campaign._diag_row(row)["overhead_s"] == 0.5


def test_diag_row_rounds_seconds_to_four_decimals() -> None:
    row = MutantDiag(
        key="k",
        path="p",
        lineno=1,
        operator="op",
        status="s",
        covering=1,
        tests_run=2,
        setup_s=1.23456789,
        call_s=2.34567891,
        teardown_s=3.45678912,
        request_s=7.123456789,
        write_s=0.12345678,
        killer="",
    )
    d = campaign._diag_row(row)
    assert d["setup_s"] == round(1.23456789, 4)
    assert d["call_s"] == round(2.34567891, 4)
    assert d["teardown_s"] == round(3.45678912, 4)
    assert d["write_s"] == round(0.12345678, 4)
    assert d["request_s"] == round(7.123456789, 4)
    overhead = max(0.0, 7.123456789 - (1.23456789 + 2.34567891 + 3.45678912))
    assert d["overhead_s"] == round(overhead, 4)


# --------------------------------------------------------------------------- report_detailed
def test_report_detailed_no_rows(capsys: pytest.CaptureFixture[str]) -> None:
    campaign.report_detailed(Diagnostics(), None)
    assert "no mutant executed" in capsys.readouterr().err


def test_report_detailed_arithmetic(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rows = [
        _diag("k1", "lang:binop", 5, request=20.0),
        _diag("k2", "lang:compare", 3, request=10.0),
    ]
    path = tmp_path / "detailed.json"
    campaign.report_detailed(Diagnostics(rows), path)
    out = capsys.readouterr().err
    # n=2, write=2, setup=4, call=6, teardown=8, overhead=12, grand=32
    assert "wall: 32.0s" in out
    assert "mean 4.0" in out
    assert "max 5" in out
    assert "1000ms/mutant" in out  # share(write) = 1000*2/2
    assert "15000ms/mutant" in out  # by_operator "lang": 1000*(20+10)/2
    assert "dominant: fork + bus overhead" in out  # overhead 12 > setup 4, call 6
    assert "detailed.json" in out  # (JSON: path) not (JSON: -)
    assert '"setup_s": 2.0' in path.read_text(encoding="utf-8")


def test_report_detailed_zero_costs_default_grand_to_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        MutantDiag("k", "p", 1, "op", "s", 1, 0, 0.0, 0.0, 0.0, 0.0, 0.0, ""),
    ]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert "wall: 1.0s" in out


def test_report_detailed_fractional_overhead(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A positive overhead below 1.0 distinguishes ``max(0.0, ...)`` from ``max(1.0, ...)``."""
    rows = [MutantDiag("k", "p", 1, "op", "s", 1, 1, 1.0, 1.0, 1.0, 3.5, 0.0, "")]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    # grand = write(0) + setup(1) + call(1) + teardown(1) + overhead(0.5) = 3.5
    assert "wall: 3.5s" in out


def test_report_detailed_percent_rounds_from_the_hundred_scale(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``100 * x / grand`` vs ``101 * x / grand`` (and ``* grand``) round to different whole percents."""
    # write=1, setup=0.5, call=0.5, teardown=0, overhead=1 -> grand=3; dominant overhead=1 -> 100*1/3 = 33%
    rows = [MutantDiag("k", "p", 1, "op", "s", 1, 1, 0.5, 0.5, 0.0, 2.0, 1.0, "")]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert "wall: 3.0s" in out
    assert "33%" in out  # share(write) percent and the dominant percent


def test_report_detailed_distinct_share_and_dominant_percents(capsys: pytest.CaptureFixture[str]) -> None:
    """``share`` and the dominant line compute separate percents; ``* grand`` flips each on its own line."""
    # write=1, setup=2, call=3, teardown=1, overhead=3 -> grand=10
    # share(write) = 10%, dominant(overhead) = 30%
    rows = [MutantDiag("k", "p", 1, "op", "s", 1, 1, 2.0, 3.0, 1.0, 9.0, 1.0, "")]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert "10%" in out  # share(write) 100*1/10; the *grand mutant prints 1000%
    assert "(30% of the wall)" in out  # dominant 100*3/10; the *grand mutant prints 300%


def test_report_detailed_groups_by_operator_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    """``partition(":")[0]`` groups by the part before the first colon; ``[1]`` would group by the colon itself."""
    rows = [_diag("k1", "lang:binop", 1, request=10.0)]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert "lang" in out  # the operator-class label; partition(":")[1] yields ":" instead


def test_report_detailed_sorts_operators_by_total_time(capsys: pytest.CaptureFixture[str]) -> None:
    """``key=lambda pair: -sum(pair[1])`` lists the heaviest operator first; the default sort would go alphabetical."""
    rows = [_diag("k1", "aaa:op", 1, request=1.0), _diag("k2", "zzz:op", 1, request=10.0)]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert out.index("zzz") < out.index("aaa")


def test_report_detailed_share_and_dominant_percent_scale(capsys: pytest.CaptureFixture[str]) -> None:
    """``100 * x / grand`` and ``101 * x / grand`` round differently at the hundreds scale on each line."""
    # write=1, setup=0.5, call=0.5, teardown=0, overhead=1 -> grand=3; share(write) and dominant are both 33%.
    rows = [MutantDiag("k", "p", 1, "op", "s", 1, 1, 0.5, 0.5, 0.0, 2.0, 1.0, "")]
    campaign.report_detailed(Diagnostics(rows), None)
    out = capsys.readouterr().err
    assert "ms/mutant   33%" in out  # share 100*1/3 rounds to 33; 101*1/3 rounds to 34
    assert "(33% of the wall)" in out  # dominant 100*1/3 rounds to 33; 101*1/3 rounds to 34


def test_report_detailed_json_escapes_non_ascii(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """``ensure_ascii=False`` keeps the raw codepoint; the mutant escapes it."""
    rows = [MutantDiag("k", "p", 1, "café", "s", 1, 1, 1.0, 1.0, 1.0, 4.0, 0.0, "")]
    path = tmp_path / "detailed.json"
    campaign.report_detailed(Diagnostics(rows), path)
    assert '"café"' in path.read_text(encoding="utf-8")


def test_report_detailed_json_uses_compact_indent_zero(tmp_path: Path) -> None:
    """``indent=0`` writes each object on one line at column zero; ``0->1`` indents the fields."""
    rows = [_diag("k1", "lang:binop", 1, request=10.0)]
    path = tmp_path / "detailed.json"
    campaign.report_detailed(Diagnostics(rows), path)
    raw = path.read_text(encoding="utf-8")
    assert "\n{\n" in raw  # indent=0 opens the object at column zero; indent=1 would emit "\n {\n"


# --------------------------------------------------------------------------- _covered_costs
def test_covered_costs_returns_empty_when_no_initial_costs() -> None:
    assert campaign._covered_costs(["a"], None) == {}


def test_covered_costs_keeps_finite_nonnegative_costs() -> None:
    initial = {"a": 1.5, "b": float("inf"), "c": float("nan"), "d": -1.0, "e": 0.0}
    assert campaign._covered_costs(["a", "b", "c", "d", "e"], initial) == {"a": 1.5, "e": 0.0}


# --------------------------------------------------------------------------- _drain_confirmations
def test_drain_confirmations_keeps_only_terminal_warm_verdicts() -> None:
    q: queue.Queue[partition.Work] = queue.Queue()
    results: partition.ResultsByIndex = {
        0: _result(mutant("src/x.py", 1), MutantStatus.KILLED),
        1: _result(mutant("src/x.py", 2), MutantStatus.SURVIVED),
        2: _result(mutant("src/x.py", 3), MutantStatus.TIMEOUT),
    }
    for index in range(4):
        q.put(work(index, mutant("src/x.py", index + 1)))
    confirmation = campaign._drain_confirmations(q, results)
    assert [item.index for item in confirmation] == [0, 1]


# --------------------------------------------------------------------------- run_lane
def test_run_lane_empty_work_returns_zero_times(tmp_path: Path) -> None:
    pool = [fake_daemon(tmp_path)]
    got = _lane(tmp_path, [], pool)
    assert got == (0.0, 0.0)


def test_run_lane_clears_stale_verdict_before_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    results: partition.ResultsByIndex = {0: _result(item.mutant, MutantStatus.SURVIVED)}
    pool = [fake_daemon(tmp_path)]
    survived = 0

    def fake_worker_loop(
        _daemon: Daemon,
        pending: queue.Queue[partition.Work],
        results_: partition.ResultsByIndex,
        progress: CampaignProgress,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        nonlocal survived
        try:
            taken = pending.get_nowait()
        except queue.Empty:
            return
        if survived == 0:
            survived += 1
            pending.put(taken)  # transport failure: requeue once
            return
        survived += 1
        _survive(taken, results_, progress)

    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", fake_worker_loop)

    _lane(tmp_path, [item], pool, results)

    assert survived == 2  # the stale verdict was popped, so the item was actually re-run


def test_run_lane_awaits_the_pool_and_calibration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path, address="main")]
    awaited: list[list[str]] = []

    def record_await(targets: list[Daemon]) -> None:
        awaited.append([d.address for d in targets])

    def missing_costs(_w: list[partition.Work], _c: dict[str, float]) -> list[str]:
        return ["tests/x_test.py::test_x"]

    def spawn_cal(*a: object, **k: object) -> list[Daemon]:
        return [fake_daemon(tmp_path, address="cal")]

    def calibrate(pool: list[Daemon], _w: list[partition.Work], _c: dict[str, float]) -> list[Daemon]:
        return pool

    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs)
    monkeypatch.setattr(campaign, "spawn_pool", spawn_cal)
    monkeypatch.setattr(campaign, "await_pool", record_await)
    monkeypatch.setattr(campaign, "calibrate_costs", calibrate)
    monkeypatch.setattr(campaign, "worker_loop", survive_loop)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)

    _lane(tmp_path, [item], pool)

    assert awaited == [["main", "cal"]]


def survive_loop(
    _daemon: Daemon,
    pending: queue.Queue[partition.Work],
    results: partition.ResultsByIndex,
    progress: CampaignProgress,
    *_args: object,
    **_kwargs: object,
) -> None:
    try:
        taken = pending.get_nowait()
    except queue.Empty:
        return
    _survive(taken, results, progress)


def test_run_lane_calibrates_and_populates_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path)]
    costs: dict[str, float] = {}

    def missing_costs(_w: list[partition.Work], _c: dict[str, float]) -> list[str]:
        return ["tests/x_test.py::test_x"]

    def spawn_cal(*a: object, **k: object) -> list[Daemon]:
        return [fake_daemon(tmp_path, address="cal")]

    def calibrate(pool: list[Daemon], _w: list[partition.Work], c: dict[str, float]) -> list[Daemon]:
        c.update({"tests/x_test.py::test_x": 0.5})
        return pool

    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs)
    monkeypatch.setattr(campaign, "spawn_pool", spawn_cal)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", calibrate)
    monkeypatch.setattr(campaign, "worker_loop", survive_loop)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)

    _lane(tmp_path, [item], pool, costs=costs)

    assert costs == {"tests/x_test.py::test_x": 0.5}


def test_run_lane_publishes_cold_calibration_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path)]
    store = Store(tmp_path / "campaign.sqlite")
    put: list[dict[str, float]] = []

    def record_put(costs: dict[str, float]) -> None:
        put.append(dict(costs))

    def missing_costs(_w: list[partition.Work], _c: dict[str, float]) -> list[str]:
        return ["tests/x_test.py::test_x"]

    def spawn_cal(*a: object, **k: object) -> list[Daemon]:
        return [fake_daemon(tmp_path, address="cal")]

    def calibrate(pool: list[Daemon], _w: list[partition.Work], c: dict[str, float]) -> list[Daemon]:
        c.update({"tests/x_test.py::test_x": 0.5})
        return pool

    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs)
    monkeypatch.setattr(campaign, "spawn_pool", spawn_cal)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", calibrate)
    monkeypatch.setattr(campaign, "worker_loop", survive_loop)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(store, "put_costs", record_put)

    try:
        campaign.run_lane(
            [item],
            {},
            CampaignProgress(1, ProgressMode.NONE),
            Timing(),
            {},
            store,
            pool=pool,
            address_base=str(tmp_path / "mutate.sock"),
            scratch=tmp_path / "run",
            cross_kill=False,
            mutant_timeout=8.0,
            finalize_timeout=True,
        )
    finally:
        store.close()

    assert put == [{"tests/x_test.py::test_x": 0.5}]


def test_run_lane_times_are_elapsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path)]
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", survive_loop)
    startup, run_ = _lane(tmp_path, [item], pool)
    assert 0.0 <= startup < 1.0
    assert 0.0 <= run_ < 1.0


def test_run_lane_rotates_the_drain_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]
    first_attempt = threading.Event()
    failed = threading.Event()
    wrong_order: list[str] = []

    def fake_worker_loop(
        target: Daemon,
        pending: queue.Queue[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        if not failed.is_set():
            if target.address == "sock-1":
                first_attempt.wait(timeout=1.0)
            try:
                taken = pending.get_nowait()
            except queue.Empty:
                return
            if target.address == "sock-0":
                first_attempt.set()
                time.sleep(0.05)
                failed.set()
                pending.put(taken)
                return
            _survive(taken, results, progress)
        else:
            try:
                taken = pending.get_nowait()
            except queue.Empty:
                return
            if target.address == "sock-0":
                wrong_order.append(target.address)
                return
            _survive(taken, results, progress)

    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", fake_worker_loop)
    results: partition.ResultsByIndex = {}

    _lane(tmp_path, [item], pool, results)

    assert wrong_order == []  # sock-0 must not take the requeued item first in wave 1
    assert results[0].status is MutantStatus.SURVIVED


def test_run_lane_continues_until_every_item_has_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items = [work(index, mutant("src/x.py", index + 1)) for index in range(2)]
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]
    requeued = 0

    def fake_worker_loop(
        _daemon: Daemon,
        pending: queue.Queue[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        nonlocal requeued
        try:
            taken = pending.get_nowait()
        except queue.Empty:
            return
        if taken.index == 0 and requeued == 0:
            requeued += 1
            pending.put(taken)
            return
        _survive(taken, results, progress)

    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "missing_cost_nodeids", no_missing_costs)
    monkeypatch.setattr(campaign, "worker_loop", fake_worker_loop)
    results: partition.ResultsByIndex = {}

    _lane(tmp_path, items, pool, results)

    assert set(results) == {0, 1}


def test_run_lane_cleans_up_calibration_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A calibration failure still shuts the disposable pool down and removes its slots in the finally."""
    item = work(0, mutant("src/x.py", 10))
    pool = [fake_daemon(tmp_path)]
    isolation = FakeIsolation()
    shutdowns: list[list[str]] = []

    def missing_costs(_w: list[partition.Work], _c: dict[str, float]) -> list[str]:
        return ["tests/x_test.py::test_x"]

    def spawn_cal(*a: object, **k: object) -> list[Daemon]:
        return [fake_daemon(tmp_path, address="cal")]

    def calibrate(_pool: list[Daemon], _w: list[partition.Work], _c: dict[str, float]) -> list[Daemon]:
        raise RuntimeError("calibration boom")

    def record_shutdown(pool: list[Daemon]) -> None:
        shutdowns.append([d.address for d in pool])

    monkeypatch.setattr(campaign, "missing_cost_nodeids", missing_costs)
    monkeypatch.setattr(campaign, "spawn_pool", spawn_cal)
    monkeypatch.setattr(campaign, "await_pool", no_pool_action)
    monkeypatch.setattr(campaign, "calibrate_costs", calibrate)
    monkeypatch.setattr(campaign, "shutdown_pool", record_shutdown)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    with pytest.raises(RuntimeError, match="calibration boom"):
        _lane(tmp_path, [item], pool, isolation=isolation)

    assert shutdowns == [["cal"]]
    assert isolation.cleaned == [(1, "prefix")]


# --------------------------------------------------------------------------- run_campaign
def test_campaign_address_suffix_is_eight_hex_chars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item])
    address = stub.lifecycle[0].removeprefix("spawn:")
    assert address.endswith(f".run-{address.rsplit('.run-', 1)[1]}")
    assert len(address.rsplit(".run-", 1)[1]) == 8


def test_campaign_passes_reexport_to_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[object, ...], object]] = []

    def fake_partition(*args: object, **kwargs: object) -> tuple[list[partition.Work], partition.ResultsByIndex, int]:
        seen.append((args, kwargs.get("reexport")))
        return [], {}, 0

    class _Cov:
        def tests_covering(self, path: str, lineno: int, /) -> set[str]:
            _ = (path, lineno)
            return {"tests/x_test.py::test_x"}

        def files_covered_by(self, test_ids: set[str], /) -> set[str]:
            _ = test_ids
            return set()

    cov = _Cov()
    monkeypatch.setattr(campaign, "partition", fake_partition)
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(10))

    reexport: dict[tuple[str, str], str] = {("src/x.py", "thing"): "tests/x_test.py"}
    campaign.run_campaign(
        [],
        {"src/x.py": "x = 1\n"},
        cov,
        options=options(tmp_path),
        store=None,
        read_cache=False,
        reexport=reexport,
    )
    assert seen[0][1] == reexport
    # partition(mutants, sources, cov, ...): the second positional argument is the source cache, the third the
    # coverage object — never the swapped order.
    assert seen[0][0][1] == {"src/x.py": "x = 1\n"}
    assert seen[0][0][2] is cov


def test_campaign_records_the_kill_validity_since_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill record boundary ``since`` (the campaign's ``files_read_at``) reaches ``KillValidity``; a dropped
    ``since`` would leave it ``None`` and skip the changed-since check."""
    item = mutant("src/x.py", 10)
    captured: dict[str, object] = {}

    class FakeValidity:
        def __init__(self, layout: object, *, support_hash: str, since: float | None = None) -> None:
            _ = layout
            captured["support_hash"] = support_hash
            captured["since"] = since

        def warm_layout(self) -> None:
            pass

    monkeypatch.setattr(campaign, "KillValidity", FakeValidity)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item])

    assert isinstance(captured["since"], float)
    assert captured["since"] > 0.0


def test_campaign_partition_seconds_are_elapsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    captured: dict[str, float] = {}

    class FakePublisher:
        def __init__(self, **kw: object) -> None:
            pass

        def publish_initial(self) -> None:
            return

        def record_terminal(self, *a: object, **k: object) -> None:
            return

        def mark_hanging(self, *a: object, **k: object) -> None:
            return

        def publish_complete(self, timings: dict[str, float] | None = None) -> None:
            if timings is not None:
                captured.update(timings)

    monkeypatch.setattr(campaign, "StatsPublisher", FakePublisher)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, stats_path=tmp_path / "stats.json"))
    assert 0.0 <= captured["cache_partition"] < 10.0


def test_campaign_auto_daemons_is_labelled_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, daemons=0))
    assert "daemons auto ->" in capsys.readouterr().err


def test_campaign_explicit_daemons_is_labelled_numerically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, daemons=1))
    assert "daemons 1 ->" in capsys.readouterr().err


def test_campaign_persists_fresh_coverage_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    store = Store(tmp_path / "campaign.sqlite")
    store.put_costs({"tests/x_test.py::test_x": 0.1})
    try:
        run(tmp_path, [item], store=store, initial_costs={"tests/x_test.py::test_x": 10.0})
        assert store.get_costs(["tests/x_test.py::test_x"]) == {"tests/x_test.py::test_x": 10.0}
    finally:
        store.close()


def test_campaign_mean_cost_averages_known_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown covering test weighs the mean of the known costs: ``sum / len``, not ``sum * len``."""
    item = mutant("src/x.py", 10)
    covering = ("tests/x_test.py::test_a", "tests/x_test.py::test_b", "tests/x_test.py::test_c")
    captured: dict[str, dict[str, float]] = {}

    class FakePublisher:
        def __init__(self, weights: dict[str, float], **kw: object) -> None:
            captured["weights"] = dict(weights)

        def publish_initial(self) -> None:
            return

        def record_terminal(self, *a: object, **k: object) -> None:
            return

        def mark_hanging(self, *a: object, **k: object) -> None:
            return

        def publish_complete(self, timings: object = None) -> None:
            return

    monkeypatch.setattr(campaign, "StatsPublisher", FakePublisher)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item, covering)], [RunResult(0, {}, [])])
    run(
        tmp_path,
        [item],
        opts=options(tmp_path, stats_path=tmp_path / "stats.json"),
        initial_costs={covering[0]: 2.0, covering[1]: 4.0},
    )
    # mean = (2.0 + 4.0) / 2 = 3.0; weight = 2.0 + 4.0 + 3.0 = 9.0
    assert captured["weights"]["key-0"] == 9.0


def test_campaign_publishes_fresh_costs_before_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``store.put_costs(fresh_costs)`` runs before the mutants; ``pass`` would only persist at the end."""
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    store = Store(tmp_path / "campaign.sqlite")
    put: list[dict[str, float]] = []

    def record_put(costs: dict[str, float]) -> None:
        put.append(dict(costs))

    def ignore_persist(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(store, "put_costs", record_put)
    monkeypatch.setattr(campaign, "persist_final", ignore_persist)
    try:
        run(tmp_path, [item], store=store, initial_costs={"tests/x_test.py::test_x": 10.0})
    finally:
        store.close()
    assert put == [{"tests/x_test.py::test_x": 10.0}]


def test_campaign_rejects_an_incomplete_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``assert_campaign_complete`` at the end of ``run_campaign`` is a gate; ``pass`` would ship partial work."""
    item = mutant("src/x.py", 10)

    def fake_partition(*a: object, **k: object) -> tuple[list[partition.Work], partition.ResultsByIndex, int]:
        return [], {}, 0

    monkeypatch.setattr(campaign, "partition", fake_partition)
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(10))
    with pytest.raises(RuntimeError, match=r"missing=\[0\]"):
        run(tmp_path, [item])


def test_campaign_rejects_invalid_timeout_policy(tmp_path: Path) -> None:
    """A negative retry count is rejected up front; the campaign never reaches the daemons."""
    with pytest.raises(ValueError, match="timeout retries cannot be negative"):
        run(tmp_path, [], opts=options(tmp_path, timeout_retries=-1))


def test_campaign_seeds_phase_timings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``phase_timings`` seeds the campaign clock; an empty campaign still reports the seed plus its own seconds."""
    captured: dict[str, float] = {}

    class FakePublisher:
        def __init__(self, **kw: object) -> None:
            pass

        def publish_initial(self) -> None:
            return

        def record_terminal(self, *a: object, **k: object) -> None:
            return

        def mark_hanging(self, *a: object, **k: object) -> None:
            return

        def publish_complete(self, timings: dict[str, float] | None = None) -> None:
            if timings is not None:
                captured.update(timings)

    class _Cov:
        def tests_covering(self, path: str, lineno: int, /) -> set[str]:
            _ = (path, lineno)
            return {"tests/x_test.py::test_x"}

        def files_covered_by(self, test_ids: set[str], /) -> set[str]:
            _ = test_ids
            return set()

    monkeypatch.setattr(campaign, "StatsPublisher", FakePublisher)
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(10))
    campaign.run_campaign(
        [],
        {"src/x.py": "x = 1\n"},
        _Cov(),
        options=options(tmp_path, stats_path=tmp_path / "stats.json"),
        store=None,
        read_cache=False,
        phase_timings={"cache_partition": 1.5},
    )
    assert captured["cache_partition"] >= 1.5


# --------------------------------------------------------------------------- _execute
@dataclass
class _ExecuteRecord:
    lane_calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    spawns: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    shutdowns: list[list[str]] = field(default_factory=list[list[str]])
    fresh_switches: list[list[str]] = field(default_factory=list[list[str]])


def _run_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    items: list[partition.Work],
    *,
    opts: campaign.CampaignOptions,
    lane_returns: tuple[tuple[float, float], ...] = ((0.5, 1.25),),
    daemons: int = 1,
    workers: int = 1,
) -> tuple[tuple[float, float], _ExecuteRecord]:
    results: partition.ResultsByIndex = {}
    progress = CampaignProgress(len(items), ProgressMode.NONE)
    record = _ExecuteRecord()
    lane_iter = iter(lane_returns)

    def fake_lane(
        work_items: list[partition.Work],
        results_: partition.ResultsByIndex,
        progress_: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        cross_kill: bool,
        mutant_timeout: float,
        finalize_timeout: bool,
        **_kw: object,
    ) -> tuple[float, float]:
        record.lane_calls.append(
            {
                "work_count": len(work_items),
                "pool": list(pool),
                "cross_kill": cross_kill,
                "mutant_timeout": mutant_timeout,
                "finalize_timeout": finalize_timeout,
                "fresh_workers": _kw.get("fresh_workers", False),
                "reject_untouched": _kw.get("reject_untouched", False),
                "reject_unstable_kill": _kw.get("reject_unstable_kill", False),
            }
        )
        for w in work_items:
            results_[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
            progress_.advance("t", MutantStatus.SURVIVED, w.key)
        try:
            return next(lane_iter)
        except StopIteration:
            return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons_: int, workers_: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays)
        record.spawns.append((daemons_, workers_))
        return [fake_daemon(tmp_path, address=f"{address_base}.{len(record.spawns)}", index=len(record.spawns))]

    def fake_shutdown(pool: list[Daemon]) -> None:
        record.shutdowns.append([d.address for d in pool])

    def switch_fresh(pool: list[Daemon]) -> None:
        record.fresh_switches.append([d.address for d in pool])

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", fake_shutdown)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", switch_fresh)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")
    got = campaign._execute(
        items,
        results,
        progress,
        Timing(),
        {},
        None,
        options=opts,
        address_base=str(tmp_path / "sock"),
        daemons=daemons,
        workers=workers,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )
    return got, record


def test_execute_accumulates_startup_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    got, _ = _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path))
    assert got == (0.5, 1.25)


def test_execute_accumulates_concurrency_lane_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The concurrency lane's own startup/run contribute to the total; ``+=`` to ``-=`` flips the sign."""
    concurrency = work(0, _concurrency_mutant(0))
    got, _ = _run_execute(
        tmp_path,
        monkeypatch,
        [concurrency],
        opts=options(tmp_path),
        lane_returns=((0.5, 1.25), (0.25, 0.75)),
    )
    assert got == (0.75, 2.0)


def test_execute_registers_sigterm_handler_on_main_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    sigcalls: list[tuple[signal.Signals, object]] = []
    real_signal = signal.signal

    def spy(sig: signal.Signals, handler: Callable[[int, FrameType | None], None]) -> object:
        sigcalls.append((sig, handler))
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", spy)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item])
    assert any(sig == signal.SIGTERM for sig, _ in sigcalls)


def test_execute_shuts_down_both_pools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    normal = work(0, mutant("src/x.py", 10))
    concurrency = work(1, _concurrency_mutant())
    _, record = _run_execute(
        tmp_path, monkeypatch, [normal, concurrency], opts=options(tmp_path, isolation=FakeIsolation())
    )
    assert len(record.shutdowns) == 2


def _concurrency_mutant(index: int = 0) -> Mutant:
    return mutant(
        "src/x.py", 11 + index, operator="concurrency:general:asynclock-drop", start_byte=2 + index, fact=LockDrop()
    )


def test_execute_serial_concurrency_lane_when_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    normal = work(0, mutant("src/x.py", 10))
    concurrency = [work(index + 1, _concurrency_mutant(index)) for index in range(2)]
    _, record = _run_execute(
        tmp_path,
        monkeypatch,
        [normal, *concurrency],
        opts=options(tmp_path, daemons=4, workers=2),
        daemons=4,
        workers=2,
    )
    # normal lane spawns (4, 2); shared concurrency lane is serial one worker (1, 1)
    assert record.spawns == [(4, 2), (1, 1)]


@pytest.mark.parametrize(("daemons", "expected"), [(10, 4), (2, 2)])
def test_execute_concurrency_daemons_bounded_by_max_and_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, daemons: int, expected: int
) -> None:
    """Ten isolated concurrency mutants: the lane width is ``min(MAX=4, daemons, work)``. Dropping the MAX cap
    over-spawns to ``daemons``; dropping the daemon bound over-spawns to ``MAX``."""
    concurrency = [work(index, _concurrency_mutant(index)) for index in range(10)]
    _, record = _run_execute(
        tmp_path,
        monkeypatch,
        concurrency,
        opts=options(tmp_path, isolation=FakeIsolation()),
        daemons=daemons,
        workers=1,
        lane_returns=((0.5, 1.25), (0.0, 0.0)),
    )
    assert record.spawns == [(expected, 1)]


def test_execute_concurrency_daemons_bounded_by_the_work_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fewer concurrency mutants than daemons: the lane width is bounded by the work count. Dropping the
    work-count bound over-spawns to ``min(MAX, daemons)``."""
    concurrency = [work(0, _concurrency_mutant(0))]
    _, record = _run_execute(
        tmp_path,
        monkeypatch,
        concurrency,
        opts=options(tmp_path, isolation=FakeIsolation()),
        daemons=4,
        workers=1,
        lane_returns=((0.5, 1.25), (0.0, 0.0)),
    )
    assert record.spawns == [(1, 1)]


def test_execute_finalize_timeout_tracks_retries_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    _, record = _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path, timeout_retries=0))
    assert record.lane_calls[0]["finalize_timeout"] is True


def test_execute_finalize_timeout_false_with_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    _, record = _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path, timeout_retries=1))
    assert record.lane_calls[0]["finalize_timeout"] is False


def test_execute_concurrency_lane_finalize_timeout_tracks_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = work(0, mutant("src/x.py", 10))
    _, record = _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path, timeout_retries=0))
    assert record.lane_calls[1]["finalize_timeout"] is True


def test_execute_restores_pytest_addopts_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    item = work(0, mutant("src/x.py", 10))
    _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path))
    assert "PYTEST_ADDOPTS" not in os.environ


def test_execute_restores_sigterm_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sigcalls: list[tuple[signal.Signals, object]] = []
    real_signal = signal.signal

    def spy(sig: signal.Signals, handler: Callable[[int, FrameType | None], None]) -> object:
        sigcalls.append((sig, handler))
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", spy)
    item = work(0, mutant("src/x.py", 10))
    _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path))
    sigterm = [handler for sig, handler in sigcalls if sig == signal.SIGTERM]
    assert len(sigterm) == 2  # register + restore


def test_execute_sigterm_handler_raises_system_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[Callable[[int, FrameType | None], None]] = []
    real_signal = signal.signal

    def spy(sig: signal.Signals, handler: Callable[[int, FrameType | None], None]) -> object:
        if sig == signal.SIGTERM and callable(handler):
            registered.append(handler)
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", spy)
    item = work(0, mutant("src/x.py", 10))
    _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path))
    assert registered  # the on_sigterm closure was registered
    with pytest.raises(SystemExit) as excinfo:
        registered[0](signal.SIGTERM, None)
    assert excinfo.value.code == 143


def test_execute_writes_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = work(0, mutant("src/x.py", 10))
    _run_execute(tmp_path, monkeypatch, [item], opts=options(tmp_path))
    assert capsys.readouterr().err.endswith("\n")


# --------------------------------------------------------------------------- timeout retry internals
def test_timeout_retry_escalates_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None, None, None])
    results = run(tmp_path, [item], opts=options(tmp_path, timeout_retries=2))
    assert results[0].status is MutantStatus.TIMEOUT
    # 8.0, then 8*3**1=24, then 8*3**2=72 (not 8*3*2=48)
    assert stub.timeouts == [8.0, 24.0, 72.0]


def test_timeout_retry_runs_in_fresh_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None, RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, timeout_retries=1))
    assert stub.fresh == [False, True]


def test_timeout_recompute_keeps_only_timeout_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A survivor and a timeout: the retry re-runs only the still-timed-out mutant."""
    items = [work(0, mutant("src/x.py", 10)), work(1, mutant("src/x.py", 11))]
    # item 0 times out then survives on retry; item 1 survives immediately.
    stub = stub_campaign_runtime(monkeypatch, tmp_path, items, [None, RunResult(0, {}, []), RunResult(0, {}, [])])
    results = run(tmp_path, [i.mutant for i in items], opts=options(tmp_path, timeout_retries=1))
    assert [r.status for r in results] == [MutantStatus.SURVIVED, MutantStatus.SURVIVED]
    assert stub.timeouts == [8.0, 8.0, 24.0]


# --------------------------------------------------------------------------- fresh confirmation internals
def _untouched_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, retries: int) -> list[float]:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item)],
        [RunResult(0, {}, []), RunResult(0, {}, [])],
        runtime_touched=[False, True],
    )
    run(tmp_path, [item], opts=options(tmp_path, isolation=FakeIsolation(), timeout_retries=retries))
    return stub.timeouts


def test_confirmation_budget_without_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``multiplier**0 == 1`` keeps the budget at the base timeout; ``multiplier*0 == 0`` would zero it."""
    assert _untouched_confirmation(tmp_path, monkeypatch, retries=0) == [8.0, 8.0]


def test_confirmation_budget_escalates_with_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``8.0 * 3**1 == 24``, not ``8.0 / 3``."""
    assert _untouched_confirmation(tmp_path, monkeypatch, retries=1) == [8.0, 24.0]


def test_execute_confirmation_lane_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirmation lane runs cross-kill, rejects untouched, and uses fresh workers."""
    item = work(0, mutant("src/x.py", 10))
    records: list[dict[str, object]] = []

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        cross_kill: bool,
        mutant_timeout: float,
        finalize_timeout: bool,
        defer_untouched: queue.Queue[partition.Work] | None = None,
        **_kw: object,
    ) -> tuple[float, float]:
        records.append(
            {
                "cross_kill": cross_kill,
                "reject_untouched": _kw.get("reject_untouched", False),
                "reject_unstable_kill": _kw.get("reject_unstable_kill", False),
                "fresh_workers": _kw.get("fresh_workers", False),
                "mutant_timeout": mutant_timeout,
            }
        )
        for w in work_items:
            results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
            if defer_untouched is not None and cross_kill is False:
                defer_untouched.put(w)  # the normal lane defers an untouched warm verdict (provisional: no advance)
            else:
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.5, 1.25)

    def spawn_one(*a: object, **k: object) -> list[Daemon]:
        return [fake_daemon(tmp_path)]

    def switch_fresh(pool: list[Daemon]) -> None:
        return None

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", spawn_one)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", switch_fresh)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=FakeIsolation()),
        address_base=str(tmp_path / "sock"),
        daemons=1,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )
    confirmation = [r for r in records if r["cross_kill"] is True]
    assert len(confirmation) >= 1
    conf = confirmation[0]
    assert conf["reject_untouched"] is True
    assert conf["reject_unstable_kill"] is True
    assert conf["fresh_workers"] is True


# --------------------------------------------------------------------------- fresh switch and retry/confirmation bounds
def test_execute_switches_to_fresh_workers_for_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None, RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, timeout_retries=1))
    assert len(stub.fresh_transitions) == 1


def test_execute_no_fresh_switch_when_retries_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None])
    run(tmp_path, [item], opts=options(tmp_path, timeout_retries=0))
    assert stub.fresh_transitions == []


def test_execute_no_fresh_switch_without_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, timeout_retries=1))
    assert stub.fresh_transitions == []


def test_timeout_recompute_skips_cached_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``index in by_index and ...`` short-circuits cached results; ``or`` would key a cached TIMEOUT."""
    cached = mutant("src/x.py", 5)
    fresh = mutant("src/x.py", 10)
    prefilled = {0: MutantResult(cached, MutantStatus.TIMEOUT, 1, "key-0", ())}
    stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(1, fresh)],
        [None, RunResult(0, {}, [])],
        prefilled=prefilled,
        cache_hits=1,
    )
    results = run(tmp_path, [cached, fresh], opts=options(tmp_path, timeout_retries=1))
    assert results[1].status is MutantStatus.SURVIVED
    assert results[0].status is MutantStatus.TIMEOUT


def test_execute_normal_retry_bounds_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    seen: dict[int, int] = {}
    calls: list[tuple[list[int], int, bool]] = []  # (work indices, pool size, fresh workers)

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        fresh_workers: bool = False,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), fresh_workers))
        for w in work_items:
            count = seen.get(w.index, 0)
            seen[w.index] = count + 1
            if count == 0:
                results[w.index] = MutantResult(w.mutant, MutantStatus.TIMEOUT, len(w.covering), w.key, w.covering)
            else:
                results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=FakeIsolation(), timeout_retries=1, daemons=4),
        address_base=str(tmp_path / "sock"),
        daemons=4,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    retry = [c for c in calls if c[0] == [0] and c[2] is True]
    assert retry == [([0], 1, True)]


def test_execute_concurrency_retry_bounds_daemons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items = [work(0, _concurrency_mutant(0)), work(1, _concurrency_mutant(1))]
    seen: dict[int, int] = {}
    calls: list[tuple[list[int], int, bool]] = []

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        fresh_workers: bool = False,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), fresh_workers))
        for w in work_items:
            count = seen.get(w.index, 0)
            seen[w.index] = count + 1
            if count == 0 and w.index == 0:
                results[w.index] = MutantResult(w.mutant, MutantStatus.TIMEOUT, len(w.covering), w.key, w.covering)
            else:
                results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        items,
        {},
        CampaignProgress(2, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=FakeIsolation(), timeout_retries=1, daemons=4),
        address_base=str(tmp_path / "sock"),
        daemons=4,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    retry = [c for c in calls if c[0] == [0] and c[2] is True]
    assert retry == [([0], 1, True)]


def test_execute_shared_normal_retry_uses_one_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared environment retries the normal lane serially: ``else 1``, not ``else 2``."""
    item = work(0, mutant("src/x.py", 10))
    seen: dict[int, int] = {}
    calls: list[tuple[list[int], int, bool]] = []

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        fresh_workers: bool = False,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), fresh_workers))
        for w in work_items:
            count = seen.get(w.index, 0)
            seen[w.index] = count + 1
            if count == 0:
                results[w.index] = MutantResult(w.mutant, MutantStatus.TIMEOUT, len(w.covering), w.key, w.covering)
            else:
                results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, timeout_retries=1, daemons=3),
        address_base=str(tmp_path / "sock"),
        daemons=3,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    retry = [c for c in calls if c[0] == [0] and c[2] is True]
    assert retry == [([0], 1, True)]


def test_execute_empty_concurrency_retry_uses_one_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no concurrency timeout the concurrency retry group is still one daemon: ``else 1``, not ``else 2``."""
    items = [work(0, mutant("src/x.py", 10)), work(1, _concurrency_mutant(0)), work(2, _concurrency_mutant(1))]
    seen: dict[int, int] = {}
    calls: list[tuple[list[int], int, bool]] = []

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        fresh_workers: bool = False,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), fresh_workers))
        for w in work_items:
            count = seen.get(w.index, 0)
            seen[w.index] = count + 1
            if count == 0 and w.index == 0:
                results[w.index] = MutantResult(w.mutant, MutantStatus.TIMEOUT, len(w.covering), w.key, w.covering)
            else:
                results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        items,
        {},
        CampaignProgress(3, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=FakeIsolation(), timeout_retries=1, daemons=4),
        address_base=str(tmp_path / "sock"),
        daemons=4,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    # The concurrency retry group is called with empty work (no concurrency timeout) and one daemon.
    concurrency_retry = [c for c in calls if c[0] == [] and c[2] is True]
    assert concurrency_retry == [([], 1, True)]


def test_execute_accumulates_retry_lane_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry lane's startup/run contribute to the total; ``+=`` to ``-=`` flips the sign."""
    item = work(0, mutant("src/x.py", 10))
    seen: dict[int, int] = {}
    times = iter([(0.5, 1.25), (0.0, 0.0), (0.25, 0.75), (0.0, 0.0)])

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        fresh_workers: bool = False,
        **_kw: object,
    ) -> tuple[float, float]:
        for w in work_items:
            count = seen.get(w.index, 0)
            seen[w.index] = count + 1
            if count == 0:
                results[w.index] = MutantResult(w.mutant, MutantStatus.TIMEOUT, len(w.covering), w.key, w.covering)
            else:
                results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return next(times)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    got = campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, timeout_retries=1),
        address_base=str(tmp_path / "sock"),
        daemons=1,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )
    assert got == (0.75, 2.0)


def test_execute_confirmation_bounds_daemons_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The isolated confirmation lane runs one daemon per mutant, finalizes timeouts, and prints a notice."""
    item = work(0, mutant("src/x.py", 10))
    calls: list[tuple[list[int], int, bool, bool]] = []  # (work, pool, cross_kill, finalize_timeout)

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        cross_kill: bool,
        finalize_timeout: bool,
        defer_untouched: queue.Queue[partition.Work] | None = None,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), cross_kill, finalize_timeout))
        for w in work_items:
            results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
            if defer_untouched is not None and cross_kill is False:
                defer_untouched.put(w)
            else:
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=FakeIsolation(), daemons=4),
        address_base=str(tmp_path / "sock"),
        daemons=4,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    confirmation = [c for c in calls if c[2] is True and c[0]]
    assert confirmation == [([0], 1, True, True)]
    assert "fresh confirmation" in capsys.readouterr().err


def test_execute_confirmation_serial_when_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared environment confirms serially: ``count`` stays 1, not ``min(daemons, len)``."""
    items = [work(0, mutant("src/x.py", 10)), work(1, mutant("src/x.py", 11))]
    calls: list[tuple[list[int], int, bool]] = []  # (work, pool, cross_kill)

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        cross_kill: bool,
        defer_untouched: queue.Queue[partition.Work] | None = None,
        **_kw: object,
    ) -> tuple[float, float]:
        calls.append(([w.index for w in work_items], len(pool), cross_kill))
        for w in work_items:
            results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
            if defer_untouched is not None and cross_kill is False:
                defer_untouched.put(w)
            else:
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return (0.0, 0.0)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    campaign._execute(
        items,
        {},
        CampaignProgress(2, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path, isolation=NoIsolation(), daemons=4),
        address_base=str(tmp_path / "sock"),
        daemons=4,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )

    confirmation = [c for c in calls if c[2] is True and c[0]]
    assert confirmation == [([0, 1], 1, True)]


def test_execute_accumulates_confirmation_lane_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fresh-confirmation lane's startup/run contribute to the total; ``+=`` to ``-=`` flips the sign."""
    item = work(0, mutant("src/x.py", 10))
    times = iter([(0.5, 1.25), (0.0, 0.0), (0.25, 0.75), (0.0, 0.0)])

    def fake_lane(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        *,
        pool: list[Daemon],
        cross_kill: bool,
        defer_untouched: queue.Queue[partition.Work] | None = None,
        **_kw: object,
    ) -> tuple[float, float]:
        for w in work_items:
            results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
            if defer_untouched is not None and cross_kill is False:
                defer_untouched.put(w)
            else:
                progress.advance("t", MutantStatus.SURVIVED, w.key)
        return next(times)

    def fake_spawn(
        address_base: str, daemons: int, workers: int, env: EnvMap, overlays: EnvOverlays, **_kw: object
    ) -> list[Daemon]:
        _ = (env, overlays, workers)
        return [fake_daemon(tmp_path, address=f"{address_base}.{index}", index=index) for index in range(daemons)]

    monkeypatch.setattr(campaign, "run_lane", fake_lane)
    monkeypatch.setattr(campaign, "spawn_pool", fake_spawn)
    monkeypatch.setattr(campaign, "shutdown_pool", no_pool_action)
    monkeypatch.setattr(campaign, "switch_pool_to_fresh_workers", no_pool_action)
    monkeypatch.setattr(campaign, "slot_prefix", lambda: "prefix")

    got = campaign._execute(
        [item],
        {},
        CampaignProgress(1, ProgressMode.NONE),
        Timing(),
        {},
        None,
        options=options(tmp_path),
        address_base=str(tmp_path / "sock"),
        daemons=1,
        workers=1,
        covering_all=[],
        diags=None,
        killers_by_key=None,
        kill_validity=None,
        scope_watch=None,
    )
    assert got == (0.75, 2.0)


# --------------------------------------------------------------------------- run_campaign reporting
def test_campaign_reports_baseline_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], initial_costs={"tests/x_test.py::test_x": 3.0})
    assert "baseline costs: 1/1 from the coverage run" in capsys.readouterr().err


def test_campaign_reports_to_run_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item])
    out = capsys.readouterr().err
    assert "to run: 1" in out
    assert "cache: 0 hits, 1 executed" in out


def test_campaign_reports_stage_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    calls: list[tuple[float, float, float, Timing, int]] = []

    def record_timing(prep: float, startup: float, run_: float, timing: Timing, daemons: int) -> None:
        calls.append((prep, startup, run_, timing, daemons))

    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    monkeypatch.setattr(campaign, "report_timing", record_timing)
    # A fixed (startup, run) pair makes every argument slot observable: the prep slot, the startup slot and the
    # run slot each hold a distinct value, and the timing slot must stay a ``Timing`` (not a shuffled float).

    def fake_execute(
        work_items: list[partition.Work],
        results: partition.ResultsByIndex,
        _progress: CampaignProgress,
        _timing: Timing,
        _costs: dict[str, float],
        _store: Store | None,
        **_k: object,
    ) -> tuple[float, float]:
        for w in work_items:
            results[w.index] = MutantResult(w.mutant, MutantStatus.SURVIVED, len(w.covering), w.key, w.covering)
        return 2.0, 3.0

    monkeypatch.setattr(campaign, "_execute", fake_execute)

    def fake_assert_complete(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(campaign, "assert_campaign_complete", fake_assert_complete)
    run(tmp_path, [item])
    assert len(calls) == 1
    prep, startup, run_, timing, daemons = calls[0]
    assert daemons == 1
    assert startup == 2.0
    assert run_ == 3.0
    assert isinstance(timing, Timing)
    assert 0.0 <= prep < 10.0


def test_campaign_flushes_and_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    flushed: list[bool] = []
    persisted: list[tuple[object, ...]] = []
    store = Store(tmp_path / "campaign.sqlite")

    def record_flush() -> None:
        flushed.append(True)
        store._db.commit()  # a real flush keeps the store usable

    def record_persist(*a: object, **k: object) -> None:
        persisted.append(a)

    monkeypatch.setattr(campaign, "persist_final", record_persist)
    monkeypatch.setattr(store, "flush", record_flush)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    try:
        run(tmp_path, [item], store=store)
    finally:
        store.close()
    assert flushed == [True]
    assert len(persisted) == 1
    # persist_final(store, sources, costs, layout=...): the second argument is the source cache, the third the
    # cost map — never the swapped order.
    assert persisted[0][1] == {"src/x.py": "def f():\n    return 1\n"}
    assert persisted[0][2] == {}


def test_campaign_persistence_seconds_are_elapsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    captured: dict[str, float] = {}

    class FakePublisher:
        def __init__(self, **kw: object) -> None:
            pass

        def publish_initial(self) -> None:
            return

        def record_terminal(self, *a: object, **k: object) -> None:
            return

        def mark_hanging(self, *a: object, **k: object) -> None:
            return

        def publish_complete(self, timings: dict[str, float] | None = None) -> None:
            if timings is not None:
                captured.update(timings)

    monkeypatch.setattr(campaign, "StatsPublisher", FakePublisher)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [RunResult(0, {}, [])])
    run(tmp_path, [item], opts=options(tmp_path, stats_path=tmp_path / "stats.json"))
    assert 0.0 <= captured["persistence"] < 10.0


def test_campaign_cached_work_keeps_zero_daemon_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-cached campaign never runs _execute: the daemon startup timing stays 0.0, not 1.0."""
    item = mutant("src/x.py", 10)
    captured: dict[str, float] = {}

    class FakePublisher:
        def __init__(self, **kw: object) -> None:
            pass

        def publish_initial(self) -> None:
            return

        def record_terminal(self, *a: object, **k: object) -> None:
            return

        def mark_hanging(self, *a: object, **k: object) -> None:
            return

        def publish_complete(self, timings: dict[str, float] | None = None) -> None:
            if timings is not None:
                captured.update(timings)

    monkeypatch.setattr(campaign, "StatsPublisher", FakePublisher)
    prefilled = {0: MutantResult(item, MutantStatus.SURVIVED, 1, "key-0", ())}

    def fake_partition(*args: object, **kwargs: object) -> tuple[list[object], partition.ResultsByIndex, int]:
        return [], prefilled, 1

    def ignore_timing(*a: object) -> None:
        return None

    monkeypatch.setattr(campaign, "partition", fake_partition)
    monkeypatch.setattr(campaign, "resolve_workers", workers_count(10))
    monkeypatch.setattr(campaign, "report_timing", ignore_timing)
    run(tmp_path, [item], opts=options(tmp_path, stats_path=tmp_path / "stats.json"))
    assert captured["daemon_startup"] == 0.0
    assert captured["mutant_execution"] == 0.0  # t_run stays 0.0, not 1.0, when nothing executes
