"""Direct unit tests for the verdict machinery (balamut.engine.worker): timing bookkeeping, hang causality,
daemon-failure requeues, protocol-breaking mutants, and diagnostics.

The lease is driven directly against a fake daemon and a scripted sequence of request outcomes, so each branch
and every bookkeeping mutation is observable without a full campaign.
"""

from __future__ import annotations

import os
import queue
from pathlib import Path
from typing import Self

import pytest

from balamut.coverage.store import Store
from balamut.engine import worker
from balamut.engine.daemon import Daemon, Hang, RunResult
from balamut.engine.partition import KillRecord, Work
from balamut.engine.progress import CampaignProgress, ProgressMode
from balamut.engine.results import (
    CampaignExactnessError,
    CampaignScopeChangedError,
    DaemonStaleError,
    MutantResult,
    MutantStatus,
    VerdictReason,
)
from balamut.runtime.kill_deps import KillValidity
from bm_support import make_layout, write_file
from bm_support.campaign import mutant, work
from bm_support.daemons import fake_daemon
from bm_support.modules import reloaded


class _RecordingStderr:
    """Captures what ``_note`` writes and whether it flushes."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.flushed = 0

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushed += 1

    def text(self) -> str:
        return "".join(self.written)


class _RecordingLock:
    """A lock stand-in that only counts entries, so a dropped lock is observed exactly once."""

    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _make_lease(
    daemon: Daemon,
    *,
    timing: worker.Timing | None = None,
    results: dict[int, MutantResult] | None = None,
    progress: CampaignProgress | None = None,
    diags: worker.Diagnostics | None = None,
    defer_untouched: queue.Queue[Work] | None = None,
    reject_untouched: bool = False,
    reject_unstable_kill: bool = False,
    fresh_workers: bool = False,
    finalize_timeout: bool = True,
    cross_kill: bool = False,
    detailed: bool = False,
    store: Store | None = None,
    semantic: worker.SemanticPatterns = worker.DEFAULT_SEMANTIC,
    kill_validity: KillValidity | None = None,
) -> worker._Lease:
    return worker._Lease(
        daemon=daemon,
        results=results if results is not None else {},
        progress=progress if progress is not None else CampaignProgress(total=1, mode=ProgressMode.NONE),
        timing=timing if timing is not None else worker.Timing(),
        costs={},
        store=store,
        cross_kill=cross_kill,
        mutant_timeout=8.0,
        finalize_timeout=finalize_timeout,
        detailed=detailed,
        diags=diags,
        killers_by_key=None,
        defer_untouched=defer_untouched,
        reject_untouched=reject_untouched,
        reject_unstable_kill=reject_unstable_kill,
        fresh_workers=fresh_workers,
        kill_validity=kill_validity,
        semantic=semantic,
    )


def _assert_result_contract(result: MutantResult, item: Work, *, status: MutantStatus) -> None:
    """The MutantResult field order is a contract: ``covering`` (int), ``key``, ``covering_tests`` match the item."""
    assert result.status is status
    assert result.covering == len(result.covering_tests)
    assert result.key == item.key
    assert result.covering_tests == item.covering


def _fake_guarded(monkeypatch: pytest.MonkeyPatch, responses: list[RunResult | Hang]) -> list[dict[str, object]]:
    """Replace ``run_subset_guarded`` with a recorder that pops scripted outcomes."""
    calls: list[dict[str, object]] = []
    remaining = list(responses)

    def guarded(*_args: object, **kwargs: object) -> RunResult | Hang:
        calls.append({key: kwargs.get(key) for key in ("full", "timeout", "label", "detailed", "fresh_workers")})
        return remaining.pop(0)

    monkeypatch.setattr(worker, "run_subset_guarded", guarded)
    return calls


def _fake_respawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    respawns: list[str] = []

    def record_respawn(daemon: Daemon) -> None:
        respawns.append(daemon.address)

    monkeypatch.setattr(worker, "respawn_daemon", record_respawn)
    return respawns


@pytest.fixture(autouse=True)
def retired(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The daemons whose persistent pytest worker a lease retired; no test reaches a real daemon this way."""
    addresses: list[str] = []

    def record_retire(daemon: Daemon) -> None:
        addresses.append(daemon.address)

    monkeypatch.setattr(worker, "retire_persistent_pool", record_retire)
    return addresses


def _clock(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    it = iter(values)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(it))


def test_timing_add_accumulates_write_request_count_and_costs() -> None:
    timing = worker.Timing()
    timing.add(2.0, 3.0)
    timing.add(0.5, 1.5, {"tests/x_test.py::test_x": 1.0})

    assert timing.write == 2.5
    assert timing.request == 4.5
    assert timing.n == 2
    assert timing.costs == {"tests/x_test.py::test_x": 1.0}


def test_timing_add_holds_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    timing = worker.Timing()
    monkeypatch.setattr(timing, "lock", lock)
    timing.add(1.0, 1.0)

    assert lock.entered == 1


def test_scope_abort_without_a_fatal_queue_raises_the_change() -> None:
    with pytest.raises(CampaignScopeChangedError) as caught:
        worker.campaign_scope_abort(None, ("src/x.py",))

    assert caught.value.paths == ("src/x.py",)


def test_worker_loop_raises_an_exactness_error_without_a_fatal_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending: queue.Queue[Work] = queue.Queue()
    pending.put(work(0, mutant("src/x.py", 10)))

    class FakeLease:
        def __init__(self, **kwargs: object) -> None:
            self.daemon = kwargs["daemon"]

        def run(self, _item: Work) -> None:
            raise CampaignExactnessError("no exact verdict")

    monkeypatch.setattr(worker, "_Lease", FakeLease)

    with pytest.raises(CampaignExactnessError, match="no exact verdict"):
        worker.worker_loop(
            fake_daemon(tmp_path),
            pending,
            {},
            CampaignProgress(total=1, mode=ProgressMode.NONE),
            worker.Timing(),
            {},
            None,
            cross_kill=False,
            mutant_timeout=8.0,
            finalize_timeout=True,
        )

    assert pending.empty()


def test_red_paired_original_without_evidence_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rc 1 of the paired original with neither a failing report nor crash evidence proves nothing."""
    item = work(0, mutant("src/x.py", 10))
    _fake_guarded(monkeypatch, [RunResult(1, {}, ["tests/x_test.py::test_x"]), RunResult(1, {}, [])])

    with pytest.raises(CampaignExactnessError, match="a paired original rc=1"):
        _make_lease(fake_daemon(tmp_path)).run(item)


def test_timing_defaults_are_zero() -> None:
    """Re-import so the cold class-body defaults run; ``0.0``/``0`` must not drift to ``1.0``/``1``."""
    with reloaded(worker):
        timing = worker.Timing()
        assert timing.write == 0.0
        assert timing.request == 0.0
        assert timing.n == 0


def test_diagnostics_add_appends_rows_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    diags = worker.Diagnostics()
    monkeypatch.setattr(diags, "lock", lock)
    item = work(0, mutant("src/x.py", 10))
    sub = RunResult(0, {}, [])
    result = MutantResult(item.mutant, MutantStatus.KILLED, 1, item.key, item.covering)
    row = worker._diag(item, result, sub, request_s=1.0, write_s=0.5)
    diags.add(row)

    assert diags.rows == [row]
    assert lock.entered == 1


def test_note_writes_the_message_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    worker._note("hello")

    assert stream.text() == "\n  hello\n"
    assert stream.flushed == 1


def test_diag_defaults_missing_phase_durations_to_zero() -> None:
    item = work(0, mutant("src/x.py", 10))
    sub = RunResult(0, {}, [], phases={"tests/x_test.py::test_x": {}})
    result = MutantResult(item.mutant, MutantStatus.KILLED, 1, item.key, item.covering, ("tests/x_test.py::test_x",))

    diag = worker._diag(item, result, sub, request_s=2.0, write_s=1.0)

    assert diag.setup_s == 0.0
    assert diag.call_s == 0.0
    assert diag.teardown_s == 0.0
    assert diag.tests_run == 1


def test_hang_verdict_unconfirmed_touch_reports_the_hung_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    hang = Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    def fail(*_args: object, **_kwargs: object) -> RunResult | Hang:
        raise AssertionError("the original must not run without a touch")

    monkeypatch.setattr(worker, "run_subset_guarded", fail)

    killer, reason = worker.hang_verdict(daemon, hang, touched=False, costs={}, timeout=1.0, label="src/x.py:10")

    assert killer is None
    assert reason is VerdictReason.HANG_UNCONFIRMED
    assert "test=tests/x_test.py::test_x" in stream.text()


def test_hang_verdict_unknown_hung_test_is_unconfirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    hang = Hang(ordered=(), completed=frozenset())
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    killer, reason = worker.hang_verdict(daemon, hang, touched=True, costs={}, timeout=1.0, label="src/x.py:10")

    assert killer is None
    assert reason is VerdictReason.HANG_UNCONFIRMED
    assert "test=?" in stream.text()


def test_hang_verdict_original_hang_respawns_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    hang = Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())
    calls: list[dict[str, object]] = []
    respawns = _fake_respawn(monkeypatch)
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    def guarded(*_args: object, **kwargs: object) -> RunResult | Hang:
        calls.append(kwargs)
        return Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())

    monkeypatch.setattr(worker, "run_subset_guarded", guarded)

    killer, reason = worker.hang_verdict(daemon, hang, touched=True, costs={}, timeout=1.0, label="src/x.py:10")

    assert killer is None
    assert reason is VerdictReason.ORIGINAL_HANG
    assert respawns == [daemon.address]
    assert "hangs on the original too" in stream.text()
    assert calls[0]["full"] is True
    assert calls[0]["fresh_workers"] is True


def test_hang_verdict_original_failure_is_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    hang = Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    def original_failure(*a: object, **k: object) -> RunResult:
        return RunResult(1, {}, [])

    monkeypatch.setattr(worker, "run_subset_guarded", original_failure)

    killer, reason = worker.hang_verdict(daemon, hang, touched=True, costs={}, timeout=1.0, label="src/x.py:10")

    assert killer is None
    assert reason is VerdictReason.ORIGINAL_FAILED
    assert "fails on the original" in stream.text()


def test_hang_verdict_causal_hang_is_a_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    hang = Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    def hang_result(*a: object, **k: object) -> RunResult:
        return RunResult(0, {}, [])

    monkeypatch.setattr(worker, "run_subset_guarded", hang_result)

    killer, reason = worker.hang_verdict(daemon, hang, touched=True, costs={}, timeout=1.0, label="src/x.py:10")

    assert killer == "tests/x_test.py::test_x"
    assert reason is VerdictReason.HANG
    assert "hang-kill" in stream.text()


def test_requeue_after_failure_notes_and_requeues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    item = work(0, mutant("src/x.py", 10))
    pending: queue.Queue[Work] = queue.Queue()
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    worker.requeue_after_failure(daemon, item, RuntimeError("boom"), pending)

    assert pending.get_nowait() is item
    assert "failed on src/x.py:10" in stream.text()


def test_worker_loop_requeues_on_daemon_staleness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    item = work(0, mutant("src/x.py", 10))
    pending: queue.Queue[Work] = queue.Queue()
    pending.put(item)
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    class FakeLease:
        def __init__(self, **kwargs: object) -> None:
            self.daemon = kwargs["daemon"]

        def run(self, _item: Work) -> None:
            raise DaemonStaleError("stale")

    monkeypatch.setattr(worker, "_Lease", FakeLease)

    worker.worker_loop(
        daemon,
        pending,
        {},
        CampaignProgress(total=1, mode=ProgressMode.NONE),
        worker.Timing(),
        {},
        None,
        cross_kill=False,
        mutant_timeout=8.0,
        finalize_timeout=True,
    )

    assert pending.get_nowait() is item
    assert "failed on src/x.py:10" in stream.text()


def test_worker_loop_requeues_on_transport_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = fake_daemon(tmp_path)
    item = work(0, mutant("src/x.py", 10))
    pending: queue.Queue[Work] = queue.Queue()
    pending.put(item)
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)

    class FakeLease:
        def __init__(self, **kwargs: object) -> None:
            self.daemon = kwargs["daemon"]

        def run(self, _item: Work) -> None:
            raise OSError("socket dropped")

    monkeypatch.setattr(worker, "_Lease", FakeLease)

    worker.worker_loop(
        daemon,
        pending,
        {},
        CampaignProgress(total=1, mode=ProgressMode.NONE),
        worker.Timing(),
        {},
        None,
        cross_kill=False,
        mutant_timeout=8.0,
        finalize_timeout=True,
    )

    assert pending.get_nowait() is item
    assert "failed on src/x.py:10" in stream.text()


def test_lease_marks_protocol_breaking_mutant_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}

    def break_protocol(*_args: object, **_kwargs: object) -> RunResult | Hang:
        raise CampaignExactnessError("broken protocol")

    monkeypatch.setattr(worker, "run_subset_guarded", break_protocol)
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(daemon, timing=timing, results=results).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.UNSUPPORTED)
    assert result.killed_by == ()
    assert timing.n == 1
    assert timing.write == 1.0
    assert timing.request == 2.0


def test_process_failure_with_green_rc_is_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}

    def green_rc(*a: object, **k: object) -> RunResult:
        return RunResult(0, {"x": 0.5}, [], process_failures=["tests/x_test.py::test_x"])

    monkeypatch.setattr(worker, "run_subset_guarded", green_rc)
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(daemon, timing=timing, results=results).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.UNSUPPORTED)
    assert result.killed_by == ()
    # The unsupported verdict still records the write and request split and the observed durations.
    assert timing.n == 1
    assert timing.write == 1.0
    assert timing.request == 2.0
    assert timing.costs == {"x": 0.5}


def test_lease_run_records_write_request_and_cost_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    calls = _fake_guarded(
        monkeypatch,
        [RunResult(1, {"a": 0.5}, ["tests/x_test.py::test_x"]), RunResult(0, {"b": 0.25}, [])],
    )
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(daemon, timing=timing, results=results).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.KILLED)
    assert result.killed_by == ("tests/x_test.py::test_x",)
    assert timing.write == 1.0
    assert timing.request == 2.0
    assert timing.n == 1
    assert timing.costs == {"a": 0.5, "b": 0.25}
    assert calls[1]["full"] is True


def test_reject_untouched_kill_becomes_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    _fake_guarded(monkeypatch, [RunResult(1, {}, ["tests/x_test.py::test_x"]), RunResult(0, {}, [])])

    _make_lease(daemon, results=results, reject_untouched=True).run(item)

    assert results[0].status is MutantStatus.UNSUPPORTED
    assert results[0].killed_by == ()


def test_survivor_whose_tests_ran_the_original_code_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    defer_untouched: queue.Queue[Work] = queue.Queue()
    markers = worker._Markers.of(daemon)

    def guarded(*_a: object, **_k: object) -> RunResult:
        # One test entered the mutated code, another ran a closure built before the mutant.
        markers.touched.write_text("1", encoding="utf-8")
        markers.stale.write_text("1", encoding="utf-8")
        return RunResult(0, {}, [])

    monkeypatch.setattr(worker, "run_subset_guarded", guarded)

    _make_lease(daemon, results=results, defer_untouched=defer_untouched).run(item)

    assert results[0].status is MutantStatus.UNSUPPORTED
    assert defer_untouched.empty()  # a fresh fork inherits the same closure


def test_kill_whose_tests_also_ran_the_original_code_stands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    markers = worker._Markers.of(daemon)
    responses = [RunResult(1, {}, ["tests/x_test.py::test_x"]), RunResult(0, {}, [])]

    def guarded(*_a: object, **_k: object) -> RunResult:
        # The killer entered the mutated code; another test ran a closure built before the mutant.
        markers.touched.write_text("1", encoding="utf-8")
        markers.stale.write_text("1", encoding="utf-8")
        return responses.pop(0)

    monkeypatch.setattr(worker, "run_subset_guarded", guarded)

    _make_lease(daemon, results=results).run(item)

    _assert_result_contract(results[0], item, status=MutantStatus.KILLED)
    assert results[0].killed_by == ("tests/x_test.py::test_x",)


def test_untouched_kill_defers_for_fresh_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    defer_untouched: queue.Queue[Work] = queue.Queue()
    _fake_guarded(monkeypatch, [RunResult(1, {"a": 0.5}, ["tests/x_test.py::test_x"]), RunResult(0, {}, [])])
    _clock(monkeypatch, [10.0, 11.0, 16.0])

    _make_lease(daemon, timing=timing, results=results, defer_untouched=defer_untouched).run(item)

    assert defer_untouched.get_nowait() is item
    _assert_result_contract(results[0], item, status=MutantStatus.KILLED)
    assert timing.write == 1.0
    assert timing.request == 5.0
    assert timing.costs == {"a": 0.5}


def test_untouched_survivor_defers_for_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    defer_untouched: queue.Queue[Work] = queue.Queue()
    _fake_guarded(monkeypatch, [RunResult(0, {"a": 0.5}, [])])
    _clock(monkeypatch, [10.0, 11.0, 17.0])

    _make_lease(daemon, timing=timing, results=results, defer_untouched=defer_untouched).run(item)

    assert defer_untouched.get_nowait() is item
    _assert_result_contract(results[0], item, status=MutantStatus.SURVIVED)
    assert timing.write == 1.0
    assert timing.request == 6.0
    assert timing.costs == {"a": 0.5}


def test_detailed_diag_records_request_and_write_durations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    diags = worker.Diagnostics()
    _fake_guarded(monkeypatch, [RunResult(0, {}, [])])
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(daemon, diags=diags, detailed=True).run(item)

    assert len(diags.rows) == 1
    assert diags.rows[0].request_s == 2.0
    assert diags.rows[0].write_s == 1.0


def test_original_hang_reports_respawns_and_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    reports: list[str] = []
    advanced: list[tuple[MutantStatus, str]] = []
    _fake_guarded(
        monkeypatch,
        [
            RunResult(1, {"a": 0.5}, ["tests/x_test.py::test_x"]),
            Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset()),
        ],
    )
    respawns = _fake_respawn(monkeypatch)
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(
        daemon,
        timing=timing,
        results=results,
        progress=CampaignProgress(
            total=1,
            mode=ProgressMode.NONE,
            on_hang=reports.append,
            on_advance=lambda status, key: advanced.append((status, key)),
        ),
    ).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.TIMEOUT)
    assert result.killed_by == ()
    assert result.reason is VerdictReason.ORIGINAL_HANG
    assert reports == [item.key]
    assert advanced == [(MutantStatus.TIMEOUT, item.key)]
    assert respawns == [daemon.address]
    assert timing.write == 1.0
    assert timing.request == 2.0
    assert timing.costs == {"a": 0.5}


def test_original_hang_unfinalized_notes_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)
    _fake_guarded(
        monkeypatch,
        [
            RunResult(1, {}, ["tests/x_test.py::test_x"]),
            Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset()),
        ],
    )
    _fake_respawn(monkeypatch)

    _make_lease(daemon, results=results, finalize_timeout=False).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.TIMEOUT)
    assert result.reason is VerdictReason.ORIGINAL_HANG
    assert "sent to an isolated retry" in stream.text()


def test_process_kill_notes_the_canonical_killer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a",))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)
    _fake_guarded(
        monkeypatch,
        [RunResult(1, {}, [], process_failures=["tests/x_test.py::test_a"]), RunResult(0, {}, [])],
    )

    _make_lease(daemon, results=results).run(item)

    _assert_result_contract(results[0], item, status=MutantStatus.KILLED)
    assert results[0].killed_by == ("tests/x_test.py::test_a",)
    assert "process-kill" in stream.text()
    assert "tests/x_test.py::test_a" in stream.text()


def test_process_failure_with_original_failure_is_unstable_not_a_process_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a",))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)
    _fake_guarded(
        monkeypatch,
        [
            RunResult(1, {}, [], process_failures=["tests/x_test.py::test_a"]),
            RunResult(1, {}, ["tests/x_test.py::test_a"]),
        ],
    )

    _make_lease(daemon, results=results).run(item)

    assert results[0].status is MutantStatus.SURVIVED
    assert "original-failure" in stream.text()
    assert "tests/x_test.py::test_a" in stream.text()
    assert "process-kill" not in stream.text()


def test_a_killer_failing_on_the_original_retires_the_persistent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    # The worker that fails a killer on the original carries state an earlier request left: the next mutant must
    # not inherit it, or every later kill of the daemon is rejected the same way.
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a",))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    _fake_guarded(
        monkeypatch, [RunResult(1, {}, ["tests/x_test.py::test_a"]), RunResult(1, {}, ["tests/x_test.py::test_a"])]
    )

    _make_lease(daemon, results=results).run(item)

    assert results[0].status is MutantStatus.SURVIVED
    assert retired == [daemon.address]


def test_one_killer_failing_on_the_original_retires_the_worker_while_another_confirms_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a", "tests/x_test.py::test_b"))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    _fake_guarded(
        monkeypatch,
        [
            RunResult(1, {}, ["tests/x_test.py::test_a", "tests/x_test.py::test_b"]),
            RunResult(1, {}, ["tests/x_test.py::test_a"]),
        ],
    )

    _make_lease(daemon, results=results).run(item)

    assert results[0].status is MutantStatus.KILLED
    assert results[0].killed_by == ("tests/x_test.py::test_b",)
    assert retired == [daemon.address]


def test_a_killer_green_on_the_original_keeps_the_persistent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a",))
    daemon = fake_daemon(tmp_path)
    _fake_guarded(monkeypatch, [RunResult(1, {}, ["tests/x_test.py::test_a"]), RunResult(0, {}, [])])

    _make_lease(daemon).run(item)

    assert retired == []


@pytest.mark.parametrize(
    ("fresh_workers", "process_level"), [(True, False), (False, True)], ids=["fresh-worker-lease", "process-level-kill"]
)
def test_an_original_that_ran_in_a_new_worker_leaves_no_pool_to_retire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str], fresh_workers: bool, process_level: bool
) -> None:
    # A fresh-worker lease forks per request, and the original of a process-level kill already ran in a new fork:
    # no persistent session saw the failure. The results are built per run: the lease edits them in place.
    killer = "tests/x_test.py::test_a"
    item = work(0, mutant("src/x.py", 10), covering=(killer,))
    daemon = fake_daemon(tmp_path)
    mutant_run = RunResult(1, {}, [], process_failures=[killer]) if process_level else RunResult(1, {}, [killer])
    calls = _fake_guarded(monkeypatch, [mutant_run, RunResult(1, {}, [killer])])

    _make_lease(daemon, fresh_workers=fresh_workers).run(item)

    assert calls[1]["fresh_workers"] is True
    assert retired == []


def test_reject_unstable_kill_defers_the_mutant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    defer_untouched: queue.Queue[Work] = queue.Queue()
    _fake_guarded(
        monkeypatch,
        [RunResult(1, {"a": 0.5}, ["tests/x_test.py::test_x"]), RunResult(1, {}, ["tests/x_test.py::test_x"])],
    )
    _clock(monkeypatch, [10.0, 11.0, 15.0])

    _make_lease(
        daemon,
        timing=timing,
        results=results,
        reject_unstable_kill=True,
        defer_untouched=defer_untouched,
    ).run(item)

    assert defer_untouched.get_nowait() is item
    _assert_result_contract(results[0], item, status=MutantStatus.TIMEOUT)
    assert timing.write == 1.0
    assert timing.request == 4.0
    assert timing.costs == {"a": 0.5}


def test_original_process_failure_with_mismatched_evidence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = work(0, mutant("src/x.py", 10), covering=("tests/x_test.py::test_a",))
    daemon = fake_daemon(tmp_path)
    _fake_guarded(
        monkeypatch,
        [
            RunResult(1, {}, ["tests/x_test.py::test_a"]),
            RunResult(1, {}, [], process_failures=["tests/x_test.py::test_other"]),
        ],
    )

    with pytest.raises(CampaignExactnessError, match="expected"):
        _make_lease(daemon).run(item)


def test_hang_unfinalized_notes_and_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    reports: list[str] = []
    stream = _RecordingStderr()
    monkeypatch.setattr(worker.sys, "stderr", stream)
    _fake_guarded(monkeypatch, [Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())])
    respawns = _fake_respawn(monkeypatch)
    _clock(monkeypatch, [10.0, 11.0, 13.0])

    _make_lease(
        daemon,
        timing=timing,
        results=results,
        progress=CampaignProgress(total=1, mode=ProgressMode.NONE, on_hang=reports.append),
        finalize_timeout=False,
    ).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.TIMEOUT)
    assert result.killed_by == ()
    assert reports == [item.key]
    assert respawns == [daemon.address]
    assert "sent to an isolated retry" in stream.text()
    assert timing.write == 1.0
    assert timing.request == 2.0


def test_hang_finalized_times_the_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    timing = worker.Timing()
    results: dict[int, MutantResult] = {}
    _fake_guarded(monkeypatch, [Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())])
    _fake_respawn(monkeypatch)
    _clock(monkeypatch, [10.0, 11.0, 14.0])

    _make_lease(daemon, timing=timing, results=results, finalize_timeout=True).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.TIMEOUT)
    assert result.killed_by == ()
    assert result.reason is VerdictReason.HANG_UNCONFIRMED
    assert timing.n == 1
    assert timing.write == 1.0
    assert timing.request == 3.0


def test_hang_finalized_kill_asserts_the_result_field_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    hang = Hang(ordered=("tests/x_test.py::test_x",), completed=frozenset())
    responses: list[RunResult | Hang] = [hang, RunResult(0, {}, [])]
    touched_path = worker._Markers.of(daemon).touched

    def guarded(*_a: object, **_k: object) -> RunResult | Hang:
        # The plugin writes the touch marker on entry before the mutant hangs, so the hang is causal.
        touched_path.write_text("", encoding="utf-8")
        return responses.pop(0)

    monkeypatch.setattr(worker, "run_subset_guarded", guarded)
    _fake_respawn(monkeypatch)

    _make_lease(daemon, results=results, finalize_timeout=True).run(item)

    result = results[0]
    _assert_result_contract(result, item, status=MutantStatus.KILLED)
    assert result.killed_by == ("tests/x_test.py::test_x",)
    assert result.reason is VerdictReason.HANG


def test_lease_persists_the_verdict_with_its_semantic_patterns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = work(0, mutant("src/x.py", 10))
    daemon = fake_daemon(tmp_path)
    results: dict[int, MutantResult] = {}
    semantic: worker.SemanticPatterns = (("tenant", ("tenant_id",)),)
    store = Store(tmp_path / "store.db")
    persisted: list[tuple[object, list[str]]] = []

    def fake_persist(
        _store: object,
        _item: object,
        _result: object,
        killed_by: list[str],
        *,
        semantic: object = worker.DEFAULT_SEMANTIC,
        **_kill: object,
    ) -> None:
        persisted.append((semantic, killed_by))

    monkeypatch.setattr(worker, "persist_verdict", fake_persist)
    _fake_guarded(monkeypatch, [RunResult(0, {}, [])])  # SURVIVED -> terminal persist

    _make_lease(daemon, results=results, store=store, semantic=semantic).run(item)

    assert persisted == [(semantic, [])]
    assert results[0].status is MutantStatus.SURVIVED


def test_kill_record_needs_a_validity_hasher(tmp_path: Path) -> None:
    """A campaign without a store has no hasher: its kills keep no record."""
    lease = _make_lease(fake_daemon(tmp_path))

    assert lease._kill_record((("src/x.py", "f"),)) is None


def test_kill_record_holds_the_dependencies_and_their_hash(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    deps = (("src/x.py", "f"),)
    validity = KillValidity(make_layout(tmp_path), support_hash="support")
    lease = _make_lease(fake_daemon(tmp_path), kill_validity=validity)

    assert lease._kill_record(deps) == KillRecord(deps, validity.of(deps))
    assert lease._kill_record(None) is None


def test_kill_whose_dependency_changed_during_the_campaign_keeps_no_record(tmp_path: Path) -> None:
    """The daemon ran the version imported at the campaign start, while the hash would read the edited file."""
    path = tmp_path / "src/x.py"
    write_file(path, "def f():\n    return 1\n")
    os.utime(path, (2_000, 2_000))
    validity = KillValidity(make_layout(tmp_path), support_hash="support", since=1_000)
    lease = _make_lease(fake_daemon(tmp_path), kill_validity=validity)

    assert lease._kill_record((("src/x.py", "f"),)) is None
