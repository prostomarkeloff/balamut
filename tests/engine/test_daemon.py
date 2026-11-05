"""The pytest-fast client side of a campaign: requests, ordering, hang evidence, specs (balamut.engine.daemon)."""

from __future__ import annotations

import importlib
import json
import os
import threading
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Self

import pytest

from balamut.engine import daemon
from balamut.engine.results import CampaignExactnessError, DaemonStaleError
from balamut.runtime.kill_deps import KILL_DEPS_SUFFIX
from balamut.runtime.plugin import TOUCHED_MARKER_SUFFIX, MutantSpec, original_lease_deps, parse_mutant_spec
from bm_support.daemons import FakeProcess, fake_daemon

type Frame = dict[str, object]
type ResultSink = Callable[[daemon.CompactResult], None]

_NO_KILLERS: frozenset[str] = frozenset()


def _client(frame: Frame, results: list[daemon.CompactResult] | None = None, calls: list[Frame] | None = None):
    def compact_request(
        address: str,
        on_result: ResultSink,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        nodeids: list[str] | None = None,
        stop_on_failure: bool = False,
        fresh_workers: bool = False,
    ) -> Frame:
        if calls is not None:
            calls.append(
                {
                    "address": address,
                    "nodeids": nodeids,
                    "stop_on_failure": stop_on_failure,
                    "fresh_workers": fresh_workers,
                }
            )
        if on_progress is not None:
            on_progress(1, 1)
        for result in results or []:
            on_result(result)
        return frame

    return compact_request


def _no_wait_ready(_address: str, _proc: daemon.DaemonProcess, _deadline: float) -> None:
    return None


def test_request_uses_compact_results_and_preserves_exact_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Frame] = []
    result: daemon.CompactResult = {
        "nodeid": "tests/x_test.py::test_x",
        "outcome": "failed",
        "duration": 0.7,
        "phases": {"setup": 0.1, "call": 0.5, "teardown": 0.1},
    }
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 1, "stop_on_failure": True}, [result], calls))
    durations: daemon.CostMap = {}
    failures: list[str] = []
    progress_clock = [0.0]
    phases: daemon.PhaseMap = {}

    rc = daemon.request(
        "/tmp/mutation.sock",
        ["tests/x_test.py::test_x"],
        durations,
        failures,
        progress_clock,
        phases,
        stop_on_failure=True,
    )

    assert rc == 1
    assert calls == [
        {
            "address": "/tmp/mutation.sock",
            "nodeids": ["tests/x_test.py::test_x"],
            "stop_on_failure": True,
            "fresh_workers": False,
        }
    ]
    assert failures == ["tests/x_test.py::test_x"]
    assert durations == {"tests/x_test.py::test_x": 0.7}
    assert phases == {"tests/x_test.py::test_x": {"setup": 0.1, "call": 0.5, "teardown": 0.1}}
    assert progress_clock[0] > 0


def test_request_records_full_test_phase_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    result: daemon.CompactResult = {
        "nodeid": "test_x",
        "outcome": "passed",
        "duration": 13.3,
        "phases": {"setup": 9.0, "call": 0.3, "teardown": 4.0},
    }
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, [result]))
    durations: daemon.CostMap = {}

    assert daemon.request("sock", ["test_x"], durations, [], stop_on_failure=False) == 0
    assert durations == {"test_x": 13.3}


def test_request_rejects_malformed_compact_result(monkeypatch: pytest.MonkeyPatch) -> None:
    bad: daemon.CompactResult = {"nodeid": "tests/x_test.py::test_x", "outcome": "passed", "duration": float("nan")}
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, [bad]))

    with pytest.raises(RuntimeError, match="malformed compact result"):
        daemon.request("sock", ["tests/x_test.py::test_x"], {}, [], stop_on_failure=False)


def test_request_captures_exact_process_failure_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    nodeid = "tests/x_test.py::test_process_exit"
    frame: Frame = {"rc": 1, "summary": "worker exited", "results": [], "run_meta": {"process_failures": [nodeid]}}
    monkeypatch.setattr(daemon, "request_run_results", _client(frame))
    evidence: list[str] = []

    rc = daemon.request("sock", [nodeid], {}, [], process_failures=evidence, stop_on_failure=True)

    assert rc == 1
    assert evidence == [nodeid]


@pytest.mark.parametrize(
    "run_meta",
    [
        {"process_failures": "tests/x_test.py::test_x"},
        {"process_failures": [1]},
        {"process_failures": ["tests/x_test.py::test_x", "tests/x_test.py::test_x"]},
        {"process_failures": ["tests/other_test.py::test_other"]},
    ],
    ids=["not-a-list", "not-strings", "duplicates", "outside-the-subset"],
)
def test_request_rejects_malformed_process_failure_evidence(
    monkeypatch: pytest.MonkeyPatch, run_meta: dict[str, object]
) -> None:
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 1, "run_meta": run_meta}))

    with pytest.raises(RuntimeError, match="process-failure"):
        daemon.request("sock", ["tests/x_test.py::test_x"], {}, [], stop_on_failure=True)


def test_rc_one_without_result_or_active_item_evidence_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 1}))

    with pytest.raises(CampaignExactnessError, match="active pytest item"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=True)


def test_stale_daemon_is_a_distinct_failure_not_a_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "request_run_results", _client({"stale": True, "rc": 1}))

    with pytest.raises(DaemonStaleError):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=True)


@pytest.mark.parametrize("rc", [2, 3, 4, 5, None])
def test_non_verdict_return_codes_are_not_kills(monkeypatch: pytest.MonkeyPatch, rc: int | None) -> None:
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": rc}))

    with pytest.raises(RuntimeError, match="not a kill"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=False)


def test_unreachable_daemon_is_retried_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def refused(*_args: object, **_kwargs: object) -> Frame:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError

    monkeypatch.setattr(daemon, "request_run_results", refused)
    monkeypatch.setattr(daemon, "_CONNECT_RETRY_DELAY", 0.0)

    with pytest.raises(RuntimeError, match="unreachable"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=False)
    assert attempts == daemon._CONNECT_ATTEMPTS


def test_stop_first_and_fresh_workers_cannot_be_combined() -> None:
    with pytest.raises(RuntimeError, match="cannot be combined"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=True, fresh_workers=True)


def test_order_covering_predicted_killers_go_first() -> None:
    ordered = daemon.order_covering(
        ["cheap_a", "exp_b", "cheap_killer", "exp_c"],
        {"cheap_a": 0.01, "exp_b": 10.0, "cheap_killer": 0.01, "exp_c": 10.0},
        frozenset({"cheap_killer"}),
    )
    assert ordered == ["cheap_killer", "cheap_a", "exp_b", "exp_c"]


def test_order_covering_expensive_known_killer_still_first() -> None:
    ordered = daemon.order_covering(["cheap", "exp_killer"], {"cheap": 0.01, "exp_killer": 10.0}, {"exp_killer"})
    assert ordered == ["exp_killer", "cheap"]


def test_order_covering_no_history_is_pure_cost_order() -> None:
    assert daemon.order_covering(["a", "b", "c"], {"a": 0.01, "b": 10.0, "c": 0.02}, _NO_KILLERS) == ["a", "c", "b"]


def test_order_covering_killer_not_in_covering_is_ignored() -> None:
    assert daemon.order_covering(["b", "a"], {"a": 0.01, "b": 0.01}, frozenset({"ghost_test"})) == ["a", "b"]


def test_order_covering_deduplicates_nodeids() -> None:
    ordered = daemon.order_covering(
        ["b", "killer", "b", "killer", "a"], {"a": 0.2, "b": 0.1, "killer": 9.0}, frozenset({"killer"})
    )
    assert ordered == ["killer", "b", "a"]


type RequestCall = tuple[list[str] | None, bool, bool]


def _recording_request(calls: list[RequestCall], *, rc: int, fail: Callable[[list[str]], list[str]]):
    def fake_request(
        _address: str,
        nodeids: list[str] | None,
        _durations: daemon.CostMap,
        failures: list[str],
        _progress_clock: list[float] | None = None,
        _phases: daemon.PhaseMap | None = None,
        *,
        process_failures: list[str] | None = None,
        stop_on_failure: bool,
        fresh_workers: bool = False,
    ) -> int:
        assert process_failures in (None, [])
        calls.append((nodeids, stop_on_failure, fresh_workers))
        failures.extend(fail(nodeids or []))
        return rc

    return fake_request


def test_run_subset_sends_one_stop_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=1, fail=lambda ids: ids[:1]))

    result = daemon.run_subset(
        "sock",
        ["slow", "predicted", "cheap"],
        {"slow": 9.0, "predicted": 8.0, "cheap": 0.1},
        predicted_killers=frozenset({"predicted"}),
    )

    assert calls == [(["predicted", "cheap", "slow"], True, False)]
    assert result.rc == 1
    assert result.killed_by == ["predicted"]
    assert result.phases == {}


def test_cross_kill_sends_one_exhaustive_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=1, fail=lambda ids: [ids[0], ids[-1]]))

    result = daemon.run_subset(
        "sock",
        ["slow", "predicted", "cheap"],
        {"slow": 9.0, "predicted": 8.0, "cheap": 0.1},
        full=True,
        predicted_killers=frozenset({"predicted"}),
    )

    assert calls == [(["predicted", "cheap", "slow"], False, False)]
    assert result.killed_by == ["predicted", "slow"]


def test_fresh_worker_subset_is_exhaustive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))

    daemon.run_subset("sock", ["test_a", "test_b"], {}, fresh_workers=True)

    assert calls == [(["test_a", "test_b"], False, True)]


def test_empty_subset_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))

    assert daemon.run_subset("sock", [], {}).rc == 0
    assert calls == []


@pytest.mark.parametrize(
    ("ordered", "completed", "expected"),
    [
        (("a", "b", "c"), frozenset[str](), "a"),
        (("a", "b", "c"), frozenset({"a"}), "b"),
        (("a", "b", "c"), frozenset({"a", "b"}), "c"),
        (("a", "b", "c"), frozenset({"b"}), None),
        (("a", "b"), frozenset({"a", "b"}), None),
        ((), frozenset[str](), None),
    ],
)
def test_hang_names_the_test_it_stopped_on(
    ordered: tuple[str, ...], completed: frozenset[str], expected: str | None
) -> None:
    assert daemon.Hang(ordered=ordered, completed=completed).hung_nodeid() == expected


def test_guarded_run_returns_hang_evidence_of_the_tests_that_did_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()

    def stalled_run_subset(
        _address: str,
        nodeids: list[str],
        costs: daemon.CostMap,
        *,
        full: bool = False,
        progress_clock: list[float] | None = None,
        detailed: bool = False,
        predicted_killers: AbstractSet[str] = _NO_KILLERS,
        fresh_workers: bool = False,
        durations: daemon.CostMap | None = None,
    ) -> daemon.RunResult:
        _ = (full, progress_clock, detailed, fresh_workers)
        assert durations is not None
        durations[daemon.order_covering(nodeids, costs, predicted_killers)[0]] = 0.01
        release.wait(timeout=30)
        return daemon.RunResult(0, dict(durations), [])

    monkeypatch.setattr(daemon, "run_subset", stalled_run_subset)
    try:
        outcome = daemon.run_subset_guarded(
            fake_daemon(tmp_path),
            ["tests/b_test.py::test_b", "tests/a_test.py::test_a"],
            {"tests/a_test.py::test_a": 0.1, "tests/b_test.py::test_b": 0.2},
            full=False,
            timeout=0.2,
            label="src/x.py:10",
        )
    finally:
        release.set()

    assert outcome == daemon.Hang(
        ordered=("tests/a_test.py::test_a", "tests/b_test.py::test_b"),
        completed=frozenset({"tests/a_test.py::test_a"}),
    )


def test_guarded_run_reraises_a_daemon_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(*_args: object, **_kwargs: object) -> daemon.RunResult:
        raise ConnectionResetError("gone")

    monkeypatch.setattr(daemon, "run_subset", broken)

    with pytest.raises(ConnectionResetError):
        daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=5.0, label="x")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_guarded_run_turns_an_unexpected_harness_error_into_a_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(*_args: object, **_kwargs: object) -> daemon.RunResult:
        raise ValueError("protocol")

    monkeypatch.setattr(daemon, "run_subset", broken)

    with pytest.raises(RuntimeError, match="without a result"):
        daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=5.0, label="x")


@pytest.mark.parametrize(
    ("ordered", "durations", "evidence", "expected"),
    [
        (["a", "b"], {}, ["a"], "a"),
        (["a", "b"], {"a": 0.1}, ["b"], "b"),
    ],
)
def test_process_failure_killer_is_the_first_unreported_test(
    tmp_path: Path, ordered: list[str], durations: daemon.CostMap, evidence: list[str], expected: str
) -> None:
    assert daemon.daemon_process_failure_nodeid(fake_daemon(tmp_path), ordered, durations, evidence) == expected


def test_process_failure_evidence_must_match_the_sequential_prefix(tmp_path: Path) -> None:
    with pytest.raises(CampaignExactnessError, match="did not match"):
        daemon.daemon_process_failure_nodeid(fake_daemon(tmp_path), ["a", "b"], {}, ["b"])
    with pytest.raises(CampaignExactnessError, match="canonical killer"):
        daemon.daemon_process_failure_nodeid(fake_daemon(tmp_path, workers=2), ["a", "b"], {}, ["a"])


def test_write_request_spec_adds_a_fresh_token_without_mutating_work_spec(tmp_path: Path) -> None:
    path = tmp_path / "mutant.json"
    spec: MutantSpec = {"module": "pkg.x", "qualname": "f", "mutated_source": "def f():\n    return 2\n", "kind": "src"}

    daemon.write_request_spec(path, spec)
    first = json.loads(path.read_text(encoding="utf-8"))
    daemon.write_request_spec(path, spec)
    second = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(first["request_token"], str)
    assert first["request_token"] != second["request_token"]
    assert first["touch_path"] == f"{path}{TOUCHED_MARKER_SUFFIX}"
    assert first["deps_path"] == f"{path}{KILL_DEPS_SUFFIX}"
    assert "request_token" not in spec
    assert "touch_path" not in spec


def test_original_spec_runs_no_mutant_but_records_its_lease(tmp_path: Path) -> None:
    path = tmp_path / "mutant.json"
    deps = tmp_path / "mutant.json.deps"

    daemon.write_original_spec(path, deps)
    raw = path.read_text(encoding="utf-8")

    assert parse_mutant_spec(raw) is None
    assert original_lease_deps(raw) == str(deps)


def test_campaign_addopts_collect_only_covering_files_and_dump_hung_stacks() -> None:
    covering = ("tests/b/b_test.py::test_b[param]", "tests/a_test.py::test_a", "tests/a_test.py::TestX::test_c")

    addopts = daemon.campaign_addopts("-p balamut.runtime.plugin", covering, hang_dump_after=7.2)

    assert addopts == "-p balamut.runtime.plugin -o faulthandler_timeout=8 tests/a_test.py tests/b/b_test.py"


def test_campaign_addopts_keep_full_collection_above_the_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "COLLECTION_FILE_LIMIT", 1)

    addopts = daemon.campaign_addopts("", ("tests/a_test.py::test_a", "tests/b_test.py::test_b"), hang_dump_after=8.0)

    assert addopts == "-o faulthandler_timeout=8"


def test_respawned_daemon_gets_a_fresh_address_and_an_empty_mutant_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned timeout thread keeps retrying the old address: it must never reach the respawned daemon."""
    spawned: list[str] = []

    def fake_spawn(address: str, *_args: object, **_kwargs: object) -> FakeProcess:
        spawned.append(address)
        return FakeProcess()

    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)

    def ready(_address: str, _proc: daemon.DaemonProcess, _deadline: float) -> None:
        return None

    monkeypatch.setattr(daemon, "wait_ready", ready)
    target = fake_daemon(tmp_path, address=str(tmp_path / "pool.0"))
    target.mutant_file.write_text("{}", encoding="utf-8")

    daemon.respawn_daemon(target)
    daemon.respawn_daemon(target)

    assert spawned == [f"{tmp_path / 'pool.0'}.r1", f"{tmp_path / 'pool.0'}.r2"]
    assert target.address == f"{tmp_path / 'pool.0'}.r2"
    assert not target.mutant_file.read_text(encoding="utf-8")


def test_switching_a_pool_to_fresh_workers_sends_an_empty_fresh_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]

    daemon.switch_pool_to_fresh_workers(pool)

    assert calls == [([], False, True), ([], False, True)]


def test_a_failed_fresh_switch_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "request", _recording_request([], rc=1, fail=lambda _ids: ["x"]))
    respawned: list[daemon.Daemon] = []
    monkeypatch.setattr(daemon, "respawn_daemon", respawned.append)
    pool = [fake_daemon(tmp_path)]

    with pytest.raises(RuntimeError, match="fresh-worker mode"):
        daemon.switch_pool_to_fresh_workers(pool)

    assert len(respawned) == 1
    assert respawned[0] is pool[0]


# ---------------------------------------------------------------------------
# _record_result: zero durations, the phase-less max path, and phase validation.
# ---------------------------------------------------------------------------


def test_request_accepts_a_zero_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    result: daemon.CompactResult = {"nodeid": "test_x", "outcome": "passed", "duration": 0.0}
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, [result]))
    durations: daemon.CostMap = {}

    assert daemon.request("sock", ["test_x"], durations, [], stop_on_failure=False) == 0
    assert durations == {"test_x": 0.0}


def test_request_records_duration_without_phases_and_keeps_the_max(monkeypatch: pytest.MonkeyPatch) -> None:
    results: list[daemon.CompactResult] = [
        {"nodeid": "test_x", "outcome": "passed", "duration": 0.5},
        {"nodeid": "test_x", "outcome": "passed", "duration": 0.3},
    ]
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, results))
    durations: daemon.CostMap = {}

    assert daemon.request("sock", ["test_x"], durations, [], stop_on_failure=False) == 0
    assert durations == {"test_x": 0.5}


def test_request_rejects_an_unknown_phase_name(monkeypatch: pytest.MonkeyPatch) -> None:
    result: daemon.CompactResult = {
        "nodeid": "test_x",
        "outcome": "passed",
        "duration": 1.0,
        "phases": {"bogus": 1.0},
    }
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, [result]))

    with pytest.raises(RuntimeError, match="malformed phases"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=False)


def test_request_accepts_zero_phase_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    result: daemon.CompactResult = {
        "nodeid": "test_x",
        "outcome": "passed",
        "duration": 0.0,
        "phases": {"setup": 0.0},
    }
    monkeypatch.setattr(daemon, "request_run_results", _client({"rc": 0}, [result]))
    durations: daemon.CostMap = {}

    assert daemon.request("sock", ["test_x"], durations, [], stop_on_failure=False) == 0
    assert durations == {"test_x": 0.0}


# ---------------------------------------------------------------------------
# process_failure_suffix / daemon_process_failure_nodeid.
# ---------------------------------------------------------------------------


def test_process_failure_suffix_rejects_a_non_prefix_report() -> None:
    with pytest.raises(RuntimeError, match="exact node id"):
        daemon.process_failure_suffix(["a", "b", "c"], {"b": 0.1})


def test_process_failure_suffix_rejects_a_fully_reported_order() -> None:
    with pytest.raises(RuntimeError, match="exact node id"):
        daemon.process_failure_suffix(["a", "b"], {"a": 0.1, "b": 0.2})


def test_process_failure_mismatch_names_the_evidence(tmp_path: Path) -> None:
    with pytest.raises(CampaignExactnessError, match="expected b, got a"):
        daemon.daemon_process_failure_nodeid(fake_daemon(tmp_path), ["a", "b"], {"a": 0.1}, ["a"])


# ---------------------------------------------------------------------------
# run_subset: default duration map and the phases of each return path.
# ---------------------------------------------------------------------------


def test_run_subset_defaults_to_an_empty_duration_map(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))

    result = daemon.run_subset("sock", [], {})

    assert result.rc == 0
    assert result.durations == {}
    assert result.phases == {}
    assert result.process_failures == []
    assert calls == []


# ---------------------------------------------------------------------------
# order_covering: a missing cost defaults to zero.
# ---------------------------------------------------------------------------


def test_order_covering_treats_missing_costs_as_zero() -> None:
    ordered = daemon.order_covering(["cheap", "unknown"], {"cheap": 0.5}, _NO_KILLERS)
    assert ordered == ["unknown", "cheap"]


# ---------------------------------------------------------------------------
# campaign_addopts: the file limit boundary.
# ---------------------------------------------------------------------------


def test_campaign_addopts_includes_files_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "COLLECTION_FILE_LIMIT", 2)

    addopts = daemon.campaign_addopts("", ("a_test.py::test_a", "b_test.py::test_b"), hang_dump_after=8.0)

    assert addopts == "-o faulthandler_timeout=8 a_test.py b_test.py"


# ---------------------------------------------------------------------------
# killpg: the group and the process, for a positive pid.
# ---------------------------------------------------------------------------


def test_killpg_kills_the_process_group_then_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []

    class Proc:
        pid = 1
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            killed.append((0, 0))

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    def fake_getpgid(pid: int) -> int:
        return pid + 100

    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)

    daemon.killpg(Proc())

    assert killed == [(101, daemon.signal.SIGKILL), (0, 0)]


# ---------------------------------------------------------------------------
# request: the connect-retry sleep.
# ---------------------------------------------------------------------------


def test_unreachable_daemon_sleeps_between_connect_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    def refused(*_args: object, **_kwargs: object) -> Frame:
        raise ConnectionRefusedError

    monkeypatch.setattr(daemon, "request_run_results", refused)
    monkeypatch.setattr(daemon, "_CONNECT_RETRY_DELAY", 0.25)
    monkeypatch.setattr(daemon.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="unreachable"):
        daemon.request("sock", ["test_x"], {}, [], stop_on_failure=False)

    assert sleeps == [0.25] * daemon._CONNECT_ATTEMPTS


# ---------------------------------------------------------------------------
# respawn_daemon: kill, wait, socket cleanup and the ready await.
# ---------------------------------------------------------------------------


def test_respawn_daemon_kills_waits_and_cleans_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[object] = []
    waits: list[float | None] = []
    readied: list[tuple[str, float]] = []
    spawned: list[tuple[str, Path, int, dict[str, str], IO[bytes], bool, int]] = []

    class Proc(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            return 0

    def fake_spawn(
        address: str,
        mutant_file: Path,
        workers: int,
        env: dict[str, str],
        log: IO[bytes],
        *,
        persist_workers: bool = True,
        idle_ttl: int = 600,
    ) -> Proc:
        spawned.append((address, mutant_file, workers, env, log, persist_workers, idle_ttl))
        return Proc()

    def fake_wait_ready(address: str, _proc: daemon.DaemonProcess, deadline: float) -> None:
        readied.append((address, deadline))

    monkeypatch.setattr(daemon, "killpg", killed.append)
    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon, "wait_ready", fake_wait_ready)

    old = tmp_path / "pool.0"
    pid_file = tmp_path / "pool.0.pid"
    new_addr = tmp_path / "pool.0.r1"
    old.write_text("stale socket", encoding="utf-8")
    pid_file.write_text("123", encoding="utf-8")
    new_addr.write_text("stale new socket", encoding="utf-8")

    target = fake_daemon(tmp_path, address=str(old))
    orig_proc = Proc()
    target.proc = orig_proc
    target.mutant_file.write_text("{}", encoding="utf-8")

    daemon.respawn_daemon(target)

    assert killed == [orig_proc]
    assert waits == [5]
    assert not old.exists()
    assert not pid_file.exists()
    assert not new_addr.exists()
    assert not target.mutant_file.read_text(encoding="utf-8")
    assert readied[0][0] == str(new_addr)
    assert readied[0][1] > daemon.time.monotonic()
    assert spawned == [(str(new_addr), target.mutant_file, 1, {}, target.log, True, 600)]


def test_respawn_preserves_a_non_numeric_r_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[str] = []

    def fake_spawn(address: str, *_a: object, **_k: object) -> FakeProcess:
        spawned.append(address)
        return FakeProcess()

    def fake_wait_ready(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon, "wait_ready", fake_wait_ready)
    target = fake_daemon(tmp_path, address=str(tmp_path / "pool.rX"))

    daemon.respawn_daemon(target)

    assert spawned == [f"{tmp_path / 'pool.rX'}.r1"]


# ---------------------------------------------------------------------------
# switch_pool_to_fresh_workers: empty mutant files and the attempt boundary.
# ---------------------------------------------------------------------------


def test_switching_a_pool_to_fresh_workers_empties_mutant_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]
    for d in pool:
        d.mutant_file.write_text("{}", encoding="utf-8")

    daemon.switch_pool_to_fresh_workers(pool)

    assert calls == [([], False, True), ([], False, True)]
    assert all(not d.mutant_file.read_text(encoding="utf-8") for d in pool)


def test_a_failed_first_attempt_respawns_before_the_second_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rcs = iter([1, 0])
    calls: list[list[str] | None] = []
    respawned: list[daemon.Daemon] = []

    def fake_request(
        _address: str,
        nodeids: list[str] | None,
        _durations: daemon.CostMap,
        _failures: list[str],
        _progress_clock: list[float] | None = None,
        _phases: daemon.PhaseMap | None = None,
        *,
        process_failures: list[str] | None = None,
        stop_on_failure: bool,
        fresh_workers: bool = False,
    ) -> int:
        _ = (process_failures, stop_on_failure, fresh_workers)
        calls.append(nodeids)
        return next(rcs)

    monkeypatch.setattr(daemon, "request", fake_request)
    monkeypatch.setattr(daemon, "respawn_daemon", respawned.append)
    pool = [fake_daemon(tmp_path)]

    daemon.switch_pool_to_fresh_workers(pool)

    assert calls == [[], []]
    assert len(respawned) == 1
    assert respawned[0] is pool[0]


# ---------------------------------------------------------------------------
# spawn_daemon / kill_stale_daemon / wait_ready / spawn_pool / await_pool /
# shutdown_pool: the process-lifecycle paths previously untested.
# ---------------------------------------------------------------------------


def test_spawn_daemon_passes_persist_workers_and_a_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pops: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        pops.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    log = BytesIO()

    proc = daemon.spawn_daemon("sock", tmp_path / "m.json", 2, {}, log, persist_workers=True)

    argv, kwargs = pops[0]
    assert "--persist-workers" in argv
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is log
    assert kwargs["stderr"] is daemon.subprocess.STDOUT
    assert kwargs["env"] is not None
    assert proc is not None


def test_spawn_daemon_passes_the_idle_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pops: list[list[str]] = []

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pops.append(argv)
        return FakeProcess()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    log = BytesIO()

    daemon.spawn_daemon("sock", tmp_path / "m.json", 1, {}, log)
    daemon.spawn_daemon("sock", tmp_path / "m.json", 1, {}, log, idle_ttl=90)

    assert pops[0][pops[0].index("--ttl") + 1] == "600"
    assert pops[1][pops[1].index("--ttl") + 1] == "90"


def test_kill_stale_daemon_kills_a_matching_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    address = str(tmp_path / "sock.0")
    pid_file = tmp_path / "sock.0.pid"
    pid_file.write_text("1234\n", encoding="utf-8")

    killed_pg: list[tuple[int, int]] = []
    killed: list[tuple[int, int]] = []
    probe_kwargs: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        _ = argv
        probe_kwargs.update(kwargs)
        return SimpleNamespace(stdout=f"pytest_fast --serve --address {address}")

    def fake_which(_name: str) -> str | None:
        return "/bin/ps"

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        killed_pg.append((pgid, sig))

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "kill", fake_kill)

    daemon.kill_stale_daemon(address)

    assert not pid_file.exists()
    assert probe_kwargs["capture_output"] is True
    assert probe_kwargs["text"] is True
    assert probe_kwargs["check"] is False
    assert killed_pg == [(1234, daemon.signal.SIGKILL)]
    assert killed == [(1234, daemon.signal.SIGKILL)]
    assert "orphaned" in capsys.readouterr().err


def test_kill_stale_daemon_tolerates_a_missing_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(_name: str) -> str | None:
        return None

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    daemon.kill_stale_daemon(str(tmp_path / "sock.0"))  # must not raise


def test_kill_stale_daemon_returns_when_ps_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sock.0.pid").write_text("1234", encoding="utf-8")
    runs: list[object] = []

    def fake_which(_name: str) -> str | None:
        return None

    def fake_run(*a: object, **_k: object) -> None:
        runs.append(1)

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    daemon.kill_stale_daemon(str(tmp_path / "sock.0"))

    assert runs == []


def test_kill_stale_daemon_probes_when_pid_and_ps_are_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sock.0.pid").write_text("1234", encoding="utf-8")

    def fake_which(_name: str) -> str | None:
        return "/bin/ps"

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    probed: list[object] = []

    def fake_run(*_a: object, **_k: object) -> SimpleNamespace:
        probed.append(1)
        return SimpleNamespace(stdout="no pytest_fast here")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    daemon.kill_stale_daemon(str(tmp_path / "sock.0"))

    assert probed != []


def test_kill_stale_daemon_ignores_a_probe_without_this_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sock.0.pid").write_text("1234", encoding="utf-8")
    killed: list[object] = []

    def fake_which(_name: str) -> str | None:
        return "/bin/ps"

    def fake_kill(_pid: int, _sig: int) -> None:
        killed.append(1)

    def fake_run(*a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="pytest_fast --serve --address other")

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    monkeypatch.setattr(daemon.os, "kill", fake_kill)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    daemon.kill_stale_daemon(str(tmp_path / "sock.0"))

    assert killed == []


def test_wait_ready_reports_a_dead_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock = tmp_path / "sock"
    proc = FakeProcess()
    proc.returncode = 3
    times = iter([0.0, 11.0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(times))

    def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="died"):
        daemon.wait_ready(str(sock), proc, deadline=10.0)


def test_wait_ready_sleeps_between_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    proc = FakeProcess()
    times = iter([0.0, 0.05, 0.1])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(daemon.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="did not bind"):
        daemon.wait_ready(str(tmp_path / "sock"), proc, deadline=0.1)

    assert sleeps == [0.1, 0.1]


def test_wait_ready_does_not_poll_when_the_deadline_is_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    polls: list[int] = []

    class Proc(FakeProcess):
        def poll(self) -> int | None:
            polls.append(1)
            return None

    times = iter([100.0, 200.0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(times))

    def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="did not bind"):
        daemon.wait_ready(str(tmp_path / "sock"), Proc(), deadline=100.0)

    assert polls == []


def _spawn_pool_harness(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> tuple[list[str], list[str]]:
    spawned: list[str] = []
    stale_killed: list[str] = []

    def fake_spawn(address: str, *_a: object, **_k: object) -> FakeProcess:
        spawned.append(address)
        return FakeProcess()

    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon, "kill_stale_daemon", stale_killed.append)
    return spawned, stale_killed


def test_spawn_pool_creates_specs_logs_and_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = tmp_path / "scratch"
    _spawned, stale_killed = _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool("base", 2, 3, {"A": "1"}, (), scratch=scratch)

    run_tag = daemon.sha("base")[:8]
    assert scratch.is_dir()
    assert [d.address for d in pool] == ["base.0", "base.1"]
    assert stale_killed == ["base.0", "base.1"]
    assert pool[0].mutant_file.name == f"mutant-{run_tag}-0.json"
    assert pool[0].log.name.endswith(f"daemon-{run_tag}-0.log")
    for d in pool:
        assert d.mutant_file.exists()
        assert not d.mutant_file.read_text(encoding="utf-8")
        assert not d.log.closed
        d.log.close()
    assert "daemons: 2 x 3 workers" in capsys.readouterr().err


def test_spawn_pool_tolerates_an_existing_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool("base", 1, 1, {}, (), scratch=scratch)
    pool[0].log.close()


def test_spawn_pool_creates_nested_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "a" / "b"
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool("base", 1, 1, {}, (), scratch=scratch)
    assert scratch.is_dir()
    pool[0].log.close()


def test_spawn_pool_removes_stale_socket_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = str(tmp_path / "base")
    (tmp_path / "base.0").write_text("stale", encoding="utf-8")
    (tmp_path / "base.1").write_text("stale", encoding="utf-8")
    scratch = tmp_path / "scratch"
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool(base, 2, 1, {}, (), scratch=scratch)

    assert not (tmp_path / "base.0").exists()
    assert not (tmp_path / "base.1").exists()
    for d in pool:
        d.log.close()


def test_spawn_pool_tolerates_a_missing_stale_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = str(tmp_path / "base")
    scratch = tmp_path / "scratch"
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool(base, 1, 1, {}, (), scratch=scratch)
    pool[0].log.close()


def test_spawn_pool_accepts_enough_isolation_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool("base", 1, 1, {}, ({"X": "1"}, {"X": "2"}), scratch=scratch)
    assert len(pool) == 1
    pool[0].log.close()


def test_spawn_pool_accepts_exactly_matching_isolation_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    _spawn_pool_harness(monkeypatch, scratch)

    pool = daemon.spawn_pool("base", 2, 1, {}, ({"X": "1"}, {"X": "2"}), scratch=scratch)
    assert len(pool) == 2
    for d in pool:
        d.log.close()


def test_await_pool_waits_for_each_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[tuple[str, float]] = []

    def fake_wait_ready(address: str, _proc: daemon.DaemonProcess, deadline: float) -> None:
        waited.append((address, deadline))

    monkeypatch.setattr(daemon.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(daemon, "wait_ready", fake_wait_ready)
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]

    daemon.await_pool(pool)

    assert [a for a, _d in waited] == ["sock-0", "sock-1"]
    assert all(d > 100.0 for _a, d in waited)


def test_spawn_pool_resolves_and_stores_the_idle_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    _spawn_pool_harness(monkeypatch, scratch)

    default = daemon.spawn_pool("base", 1, 1, {}, (), scratch=scratch)
    assert default[0].idle_ttl == 600
    default[0].log.close()

    custom = daemon.spawn_pool("base", 1, 1, {}, (), scratch=scratch, idle_ttl=90)
    assert custom[0].idle_ttl == 90
    custom[0].log.close()


def test_await_pool_respawns_a_cleanly_exited_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respawned: list[daemon.Daemon] = []
    monkeypatch.setattr(daemon, "respawn_daemon", respawned.append)
    monkeypatch.setattr(daemon, "wait_ready", _no_wait_ready)
    alive = fake_daemon(tmp_path, address="alive", index=0)
    idle = fake_daemon(tmp_path, address="idle", index=1, returncode=0)

    daemon.await_pool([alive, idle])

    assert respawned == [idle]


def test_await_pool_raises_on_an_unclean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respawned: list[daemon.Daemon] = []
    monkeypatch.setattr(daemon, "respawn_daemon", respawned.append)
    broken = fake_daemon(tmp_path, returncode=3)

    with pytest.raises(RuntimeError, match="died"):
        daemon.await_pool([broken])

    assert respawned == []


def test_await_pool_respawns_when_the_ttl_fires_mid_await(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respawned: list[daemon.Daemon] = []
    monkeypatch.setattr(daemon, "respawn_daemon", respawned.append)
    target = fake_daemon(tmp_path)

    def ttl_exit(_address: str, proc: FakeProcess, _deadline: float) -> None:
        proc.returncode = 0
        raise RuntimeError("the pytest-fast daemon died (exit 0); see its log")

    monkeypatch.setattr(daemon, "wait_ready", ttl_exit)

    daemon.await_pool([target])

    assert respawned == [target]


def test_respawn_daemon_reuses_the_configured_idle_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[dict[str, object]] = []

    def fake_spawn(_address: str, *_a: object, **kwargs: object) -> FakeProcess:
        spawned.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon, "wait_ready", _no_wait_ready)
    target = fake_daemon(tmp_path)
    target.idle_ttl = 90

    daemon.respawn_daemon(target)

    assert spawned == [{"persist_workers": True, "idle_ttl": 90}]


def test_shutdown_pool_terminates_waits_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        pid = 0
        returncode: int | None = None

        def __init__(self) -> None:
            self.terminated = False
            self.waits: list[float | None] = []

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            return self.returncode or 0

    killed: list[object] = []
    monkeypatch.setattr(daemon, "killpg", killed.append)
    pool = [fake_daemon(tmp_path, address=str(tmp_path / f"sock-{index}"), index=index) for index in range(2)]
    procs: list[Proc] = []
    for d in pool:
        p = Proc()
        procs.append(p)
        d.proc = p
        d.mutant_file.write_text("{}", encoding="utf-8")
        Path(d.address).write_text("stale", encoding="utf-8")
        Path(f"{d.address}.pid").write_text("123", encoding="utf-8")
        for suffix in (TOUCHED_MARKER_SUFFIX, daemon.UNSUPPORTED_MARKER_SUFFIX, KILL_DEPS_SUFFIX):
            Path(f"{d.mutant_file}{suffix}").write_text("1", encoding="utf-8")

    daemon.shutdown_pool(pool)

    for d, p in zip(pool, procs, strict=True):
        assert p.terminated
        assert p.waits == [10]
        assert d.log.closed
        assert not d.mutant_file.read_text(encoding="utf-8")
        assert not Path(d.address).exists()
        assert not Path(f"{d.address}.pid").exists()
        for suffix in (TOUCHED_MARKER_SUFFIX, daemon.UNSUPPORTED_MARKER_SUFFIX, KILL_DEPS_SUFFIX):
            assert not Path(f"{d.mutant_file}{suffix}").exists()
    assert killed == []


def test_shutdown_pool_kills_a_daemon_that_will_not_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        pid = 0
        returncode: int | None = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.returncode = None

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            raise daemon.subprocess.TimeoutExpired("cmd", timeout or 0.0)

    killed: list[object] = []
    monkeypatch.setattr(daemon, "killpg", killed.append)
    d = fake_daemon(tmp_path, address=str(tmp_path / "sock-0"))
    d.proc = Proc()

    daemon.shutdown_pool([d])

    assert killed == [d.proc]
    assert d.log.closed


def test_shutdown_pool_tolerates_missing_marker_and_socket_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = [fake_daemon(tmp_path, address=str(tmp_path / "sock-0"))]

    daemon.shutdown_pool(pool)  # must not raise


# ---------------------------------------------------------------------------
# run_subset_guarded: the no-progress watchdog, driven by a controlled thread
# and clock so the warning/hang boundaries are exact.
# ---------------------------------------------------------------------------


class _ControlledThread:
    """A ``threading.Thread`` stand-in: runs its target synchronously and scripts ``is_alive``."""

    def __init__(self, target: Callable[[], None], *, daemon: bool = True, name: str | None = None, **_: object):
        self.target = target
        self.daemon_flag = daemon
        self.join_calls: list[float | None] = []
        self._alive_script: list[bool] = [True]

    def start(self) -> None:
        self.target()

    def is_alive(self) -> bool:
        return self._alive_script.pop(0) if self._alive_script else False

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)


class _StderrRecorder:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> None:
        self.writes.append(text)

    def flush(self) -> None:
        self.flushes += 1


def _guarded_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    alive_script: list[bool],
    monotonic_values: list[float],
    result: daemon.RunResult | None = None,
    error: BaseException | None = None,
) -> tuple[list[_ControlledThread], _StderrRecorder]:
    threads: list[_ControlledThread] = []

    def thread_factory(
        target: Callable[[], None], *, daemon: bool = False, name: str | None = None, **kwargs: object
    ) -> _ControlledThread:
        thread = _ControlledThread(target, daemon=daemon, name=name, **kwargs)
        thread._alive_script = list(alive_script)
        threads.append(thread)
        return thread

    monkeypatch.setattr(daemon.threading, "Thread", thread_factory)
    values = iter(monotonic_values)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(values))
    stderr = _StderrRecorder()
    monkeypatch.setattr(daemon.sys, "stderr", stderr)
    if error is not None:

        def broken(*_a: object, **_k: object) -> daemon.RunResult:
            raise error

        monkeypatch.setattr(daemon, "run_subset", broken)
    else:
        res = result if result is not None else daemon.RunResult(0, {}, [], {})

        def fake_run_subset(*_a: object, **_k: object) -> daemon.RunResult:
            return res

        monkeypatch.setattr(daemon, "run_subset", fake_run_subset)
    return threads, stderr


def test_guarded_run_returns_the_result_when_it_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _guarded_harness(monkeypatch, alive_script=[], monotonic_values=[0.0])

    outcome = daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=5.0, label="x")

    assert outcome == daemon.RunResult(0, {}, [], {})


def test_guarded_run_hang_warns_and_joins_the_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    threads, stderr = _guarded_harness(monkeypatch, alive_script=[True, True, True], monotonic_values=[0.0, 2.0])

    outcome = daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=1.0, label="x")

    assert isinstance(outcome, daemon.Hang)
    assert threads[0].daemon_flag is True
    assert threads[0].join_calls == [1.0]
    assert "".join(stderr.writes).count("stalled") == 1
    assert stderr.flushes == 1


def test_guarded_run_stalled_is_elapsed_not_absolute_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _guarded_harness(monkeypatch, alive_script=[True, True, True, False], monotonic_values=[0.5, 1.3])

    outcome = daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=1.0, label="x")

    assert outcome == daemon.RunResult(0, {}, [], {})


def test_guarded_run_hangs_only_strictly_after_the_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _guarded_harness(monkeypatch, alive_script=[True, True, True, False], monotonic_values=[0.0, 1.0])

    outcome = daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=1.0, label="x")

    assert outcome == daemon.RunResult(0, {}, [], {})


def test_guarded_run_hang_requires_a_live_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _guarded_harness(monkeypatch, alive_script=[True, True, False, False], monotonic_values=[0.0, 2.0])

    outcome = daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=1.0, label="x")

    assert outcome == daemon.RunResult(0, {}, [], {})


def test_guarded_run_warns_only_at_the_fraction_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _threads, stderr = _guarded_harness(
        monkeypatch, alive_script=[True, True, True, False], monotonic_values=[0.0, 0.75]
    )

    daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=1.0, label="x")

    assert "".join(stderr.writes).count("stalled") == 1


def test_guarded_run_warns_once_while_stalled_below_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _threads, stderr = _guarded_harness(
        monkeypatch, alive_script=[True, True, True, True, True, True, False], monotonic_values=[0.0, 8.0, 9.0]
    )

    daemon.run_subset_guarded(fake_daemon(tmp_path), ["t"], {}, full=False, timeout=10.0, label="x")

    assert "".join(stderr.writes).count("stalled") == 1


# ---------------------------------------------------------------------------
# Module-level constants and dataclass defaults: re-execute the module body so
# the coverage tracer measures these import-time lines.
# ---------------------------------------------------------------------------


def test_module_configuration_and_dataclass_defaults() -> None:
    # The reload re-executes the module body for the coverage tracer, but it also recreates every class; other
    # modules and test files bound the original objects at import time, so their identity is restored.
    hang, run_result, daemon_class = daemon.Hang, daemon.RunResult, daemon.Daemon
    importlib.reload(daemon)
    daemon.Hang, daemon.RunResult, daemon.Daemon = hang, run_result, daemon_class
    assert daemon._READY_TIMEOUT == 180.0
    assert daemon._IDLE_TTL_S == 600
    assert daemon._CONNECT_ATTEMPTS == 50
    assert daemon._CONNECT_RETRY_DELAY == 0.1
    assert daemon._HANG_WARN_FRACTION == 0.75
    assert daemon.COLLECTION_FILE_LIMIT == 400

    d = daemon.Daemon("sock", Path("mutant.json"), FakeProcess(), BytesIO(), 1, {})
    assert d.persist_workers is True
    assert d.respawns == 0


# ---------------------------------------------------------------------------
# Argument-order contracts of the request, spawn and suppress call sites.
# ---------------------------------------------------------------------------


def test_run_subset_passes_progress_clock_and_phases_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, object]] = []

    def fake_request(
        _address: str,
        nodeids: list[str] | None,
        _durations: daemon.CostMap,
        failures: list[str],
        progress_clock: list[float] | None = None,
        phases: daemon.PhaseMap | None = None,
        *,
        process_failures: list[str] | None = None,
        stop_on_failure: bool = False,
        fresh_workers: bool = False,
    ) -> int:
        _ = (nodeids, failures, process_failures, stop_on_failure, fresh_workers)
        calls.append((progress_clock, phases))
        return 0

    monkeypatch.setattr(daemon, "request", fake_request)
    clock = [1.0]

    daemon.run_subset("sock", ["t"], {}, detailed=True, progress_clock=clock)

    assert calls == [(clock, {})]


def test_run_subset_returns_process_failure_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(
        _address: str,
        nodeids: list[str] | None,
        _durations: daemon.CostMap,
        failures: list[str],
        _progress_clock: list[float] | None = None,
        _phases: daemon.PhaseMap | None = None,
        *,
        process_failures: list[str] | None = None,
        stop_on_failure: bool = False,
        fresh_workers: bool = False,
    ) -> int:
        _ = (nodeids, failures, stop_on_failure, fresh_workers)
        assert process_failures is not None
        process_failures.append("tests/x_test.py::test_x")
        return 1

    monkeypatch.setattr(daemon, "request", fake_request)

    result = daemon.run_subset("sock", ["tests/x_test.py::test_x"], {})

    assert result.process_failures == ["tests/x_test.py::test_x"]


def test_order_covering_predicted_killers_are_cost_ordered() -> None:
    ordered = daemon.order_covering(
        ["a_killer", "z_killer", "mid"],
        {"a_killer": 9.0, "z_killer": 0.1, "mid": 1.0},
        frozenset({"a_killer", "z_killer"}),
    )
    assert ordered == ["z_killer", "a_killer", "mid"]


def test_killpg_suppresses_a_plain_os_error_from_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        pid = 1
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_killpg(_pgid: int, _sig: int) -> None:
        raise OSError("no such process group")

    def fake_getpgid(_pid: int) -> int:
        return 1

    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)

    daemon.killpg(Proc())  # must not raise


@pytest.mark.parametrize("kill_exc", [ProcessLookupError, PermissionError])
def test_killpg_suppresses_the_fallback_kill_error(monkeypatch: pytest.MonkeyPatch, kill_exc: type[OSError]) -> None:
    class Proc:
        pid = 1
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            raise kill_exc()

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_killpg(_pgid: int, _sig: int) -> None:
        return None

    def fake_getpgid(_pid: int) -> int:
        return 1

    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)

    daemon.killpg(Proc())  # must not raise


def test_kill_stale_daemon_suppresses_a_plain_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    address = str(tmp_path / "sock.0")
    (tmp_path / "sock.0.pid").write_text("1234\n", encoding="utf-8")

    def fake_which(_name: str) -> str | None:
        return "/bin/ps"

    def fake_run(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=f"pytest_fast --serve --address {address}")

    def fake_getpgid(_pid: int) -> int:
        return 1234

    def fake_killpg(_pgid: int, _sig: int) -> None:
        raise OSError("no such process group")

    def fake_kill(_pid: int, _sig: int) -> None:
        return None

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "kill", fake_kill)

    daemon.kill_stale_daemon(address)  # must not raise


@pytest.mark.parametrize("kill_exc", [ProcessLookupError, PermissionError])
def test_kill_stale_daemon_suppresses_the_fallback_kill_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kill_exc: type[OSError]
) -> None:
    address = str(tmp_path / "sock.0")
    (tmp_path / "sock.0.pid").write_text("1234\n", encoding="utf-8")

    def fake_which(_name: str) -> str | None:
        return "/bin/ps"

    def fake_run(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=f"pytest_fast --serve --address {address}")

    def fake_getpgid(_pid: int) -> int:
        return 1234

    def fake_killpg(_pgid: int, _sig: int) -> None:
        return None

    def fake_kill(_pid: int, _sig: int) -> None:
        raise kill_exc()

    monkeypatch.setattr(daemon.shutil, "which", fake_which)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(daemon.os, "killpg", fake_killpg)
    monkeypatch.setattr(daemon.os, "kill", fake_kill)

    daemon.kill_stale_daemon(address)  # must not raise


def test_spawn_pool_passes_daemon_arguments_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    calls: list[tuple[str, Path, int, dict[str, str], IO[bytes], bool, int]] = []

    def fake_spawn(
        address: str,
        mutant_file: Path,
        workers: int,
        env: dict[str, str],
        log: IO[bytes],
        *,
        persist_workers: bool = True,
        idle_ttl: int = 600,
    ) -> FakeProcess:
        calls.append((address, mutant_file, workers, env, log, persist_workers, idle_ttl))
        return FakeProcess()

    def fake_kill_stale(_address: str) -> None:
        return None

    monkeypatch.setattr(daemon, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon, "kill_stale_daemon", fake_kill_stale)

    pool = daemon.spawn_pool("base", 2, 3, {"A": "1"}, (), scratch=scratch, persist_workers=False)
    for d in pool:
        d.log.close()

    run_tag = daemon.sha("base")[:8]
    assert calls == [
        ("base.0", scratch / f"mutant-{run_tag}-0.json", 3, {"A": "1"}, pool[0].log, False, 600),
        ("base.1", scratch / f"mutant-{run_tag}-1.json", 3, {"A": "1"}, pool[1].log, False, 600),
    ]
    assert [d.address for d in pool] == ["base.0", "base.1"]
    assert [d.workers for d in pool] == [3, 3]
    assert [d.env for d in pool] == [{"A": "1"}, {"A": "1"}]
    assert [d.persist_workers for d in pool] == [False, False]
    assert [d.idle_ttl for d in pool] == [600, 600]


def test_adopt_pool_adopts_a_live_persisted_pool(tmp_path: Path) -> None:
    """A persisted pool with pid files, live pids and bound sockets is adopted without a spawn."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    base = str(tmp_path / "pool")
    for index in range(2):
        Path(f"{base}.{index}").touch()  # the bound socket
        Path(f"{base}.{index}.pid").write_text(str(os.getpid()), encoding="utf-8")

    pool = daemon.adopt_pool(base, 2, 1, {"A": "1"}, scratch)

    assert pool is not None
    assert [d.address for d in pool] == [f"{base}.0", f"{base}.1"]
    assert [d.proc.pid for d in pool] == [os.getpid(), os.getpid()]
    assert [d.workers for d in pool] == [1, 1]
    assert [d.env for d in pool] == [{"A": "1"}, {"A": "1"}]
    run_tag = daemon.sha(base)[:8]
    assert [d.mutant_file for d in pool] == [scratch / f"mutant-{run_tag}-{index}.json" for index in range(2)]
    for d in pool:
        d.log.close()


def test_adopt_pool_declines_on_a_gap_or_a_dead_pid(tmp_path: Path) -> None:
    """A missing socket or pid file, a dead pid and a corrupt pid all decline the whole pool."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    base = str(tmp_path / "pool")
    Path(f"{base}.0").touch()
    Path(f"{base}.0.pid").write_text(str(os.getpid()), encoding="utf-8")

    assert daemon.adopt_pool(base, 2, 1, {}, scratch) is None  # index 1 absent

    Path(f"{base}.1").touch()
    Path(f"{base}.1.pid").write_text("999999999999", encoding="utf-8")  # beyond the pid range: corrupt

    assert daemon.adopt_pool(base, 2, 1, {}, scratch) is None


def test_fresh_worker_switch_uses_a_bounded_named_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[RequestCall] = []
    monkeypatch.setattr(daemon, "request", _recording_request(calls, rc=0, fail=lambda _ids: []))
    pool = [fake_daemon(tmp_path, address=f"sock-{index}", index=index) for index in range(2)]
    executor_kwargs: list[dict[str, object]] = []

    class _FakeExecutor:
        def __init__(self, **kwargs: object) -> None:
            executor_kwargs.append(kwargs)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def map(self, fn: Callable[[daemon.Daemon], None], items: list[daemon.Daemon]) -> list[None]:
            return [fn(d) for d in items]

    monkeypatch.setattr(daemon, "ThreadPoolExecutor", _FakeExecutor)

    daemon.switch_pool_to_fresh_workers(pool)

    assert executor_kwargs == [{"max_workers": 2, "thread_name_prefix": "balamut-fresh"}]
