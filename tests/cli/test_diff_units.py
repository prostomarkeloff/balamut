"""White-box unit tests for the pure helpers and boundary functions of ``balamut diff``.

These close the mutation gaps that the scripted-scenario tests leave open: the standalone helpers are exercised
directly, and the orchestrator's branch lines are hit through the same scripted ``Scenario`` boundary.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import itertools
import json
import os
import platform
import re
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from io import TextIOWrapper
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO, TypedDict

import pytest

from balamut.cli import diff
from balamut.config import DatabaseEnv, IsolationSettings, Layout, Profile
from balamut.coverage import baseline_failure, pool, substrate
from balamut.coverage import costs as pytest_costs
from balamut.coverage.measure import MeasureRequest, MeasureResult
from balamut.diff import gate as diff_gate
from balamut.diff import scope as diff_scope
from balamut.engine.stats import CampaignStats
from balamut.isolation import Isolation, IsolationError, NoIsolation
from bm_support import write_file
from bm_support.diff import (
    Row,
    Scenario,
    coverage_path,
    expected,
    main_args,
    project_profile,
    raw_mutant,
    run_main,
    section,
    write_project,
)
from bm_support.scopes import scope

_FAILED = "tests/x_test.py::test_regression"


class _RunKwargs(TypedDict):
    cwd: Path
    env: dict[str, str]
    text: bool
    stdout: TextIO


class _RunCapture(TypedDict):
    argv: list[str]
    kwargs: _RunKwargs


def _big_step_clock() -> Callable[[], float]:
    counter = itertools.count()

    def clock() -> float:
        return next(counter) * 1000.0

    return clock


def _campaign_stats(
    *, enumerated: int, status_counts: dict[str, int], timings: dict[str, float] | None = None
) -> CampaignStats:
    return CampaignStats(
        schema_version=1,
        complete=True,
        enumerated=enumerated,
        cache_resolved=0,
        executed=enumerated,
        terminal=enumerated,
        status_counts=status_counts,
        timings=timings or {},
    )


def _fingerprint(root: Path) -> str:
    pinned = scope()
    return baseline_failure.failure_fingerprint(
        expected(root),
        base_sha=pinned.base_sha,
        diff_base_sha=pinned.diff_base_sha,
        head_sha=pinned.head_sha,
        environment=diff.fingerprint_environment(diff.load_profile(root), NoIsolation(), {}),
    )


def _record(root: Path, nodeids: tuple[str, ...], *, generation: str = "") -> None:
    baseline_failure.failure_write(
        coverage_path(root),
        baseline_failure.BaselineFailureRecord(_fingerprint(root), nodeids, generation),
    )


def _read(root: Path) -> baseline_failure.BaselineFailureRecord | None:
    return baseline_failure.failure_read(coverage_path(root), expected_fingerprint=_fingerprint(root))


# --- module-level values and the type aliases (reload re-executes them) -----------------------------


def test_module_reload_re_executes_type_aliases_and_defaults() -> None:
    importlib.reload(diff)

    assert diff.JsonValue is not None
    assert diff.JsonObject is not None
    assert diff.RunState().substrate_reused is False
    assert diff.RunState().substrate_drift_files == 0
    assert diff.RunState().baseline_failure_cached is False


def test_empty_environment_is_an_empty_dict() -> None:
    assert diff._empty_environment() == {}


def test_initial_counts_are_all_zero() -> None:
    assert diff._initial_counts() == {
        "enumerated": 0,
        "cache_resolved": 0,
        "executed": 0,
        "killed": 0,
        "survived": 0,
        "timeout": 0,
        "no_coverage": 0,
        "unsupported": 0,
    }


# --- fingerprinting ----------------------------------------------------------------------------------


def test_fingerprint_keeps_non_ascii_unescaped() -> None:
    assert diff._fingerprint([("name", "café")]) == hashlib.sha256('[["name","café"]]'.encode()).hexdigest()


def test_fingerprint_environment_reads_the_database_env_names_and_defaults_missing_ones(tmp_path: Path) -> None:
    """The confirmation environment is keyed by the configured variable names, with an empty string for any
    variable the process does not provide (a dropped default would leave ``None``)."""
    profile = replace(
        project_profile(tmp_path),
        isolation=IsolationSettings(env=DatabaseEnv(host="H", port="P", database="D", user="U", password="W")),
    )
    environ: dict[str, str] = {"H": "host", "P": "port", "D": "db"}

    result = diff.fingerprint_environment(profile, NoIsolation(), environ)

    assert result["isolation"] == NoIsolation().fingerprint()
    assert result["H"] == "host"
    assert result["P"] == "port"
    assert result["D"] == "db"
    assert result["U"] == ""
    assert result["W"] == ""


def test_runtime_fingerprint_is_the_hash_of_its_components() -> None:
    entries = [
        ("python", platform.python_version()),
        ("implementation", sys.implementation.name),
        ("cache_tag", sys.implementation.cache_tag or ""),
        ("machine", platform.machine()),
    ]
    for distribution in diff._RUNTIME_DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "missing"
        entries.append((distribution, version))
    assert diff.runtime_fingerprint() == diff._fingerprint(entries)


def test_expected_metadata_is_incomplete() -> None:
    assert diff.expected_metadata(project_profile(Path.cwd()), NoIsolation()).complete is False


def test_invocation_report_keeps_non_ascii_mutate_args() -> None:
    options = diff.RunOptions(
        base="origin/main",
        base_mode=diff_scope.BaseMode.MERGE_BASE,
        mode=diff.DiffMode.FAST,
        coverage_path=Path("coverage"),
        raw_json_path=Path("raw.json"),
        report_json_path=Path("report.json"),
        agent_json=False,
        rebuild_substrate=False,
        mutate_args=("--name", "café"),
    )
    digest = diff._invocation_report(options)["mutate_args_digest"]
    expected = hashlib.sha256(
        json.dumps(("--name", "café"), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == expected


# --- path/estimate/banner helpers --------------------------------------------------------------------


def test_paths_brief_counts_the_extra_paths() -> None:
    assert diff._paths_brief([f"p{index}" for index in range(11)]) == "p0, p1, p2, p3, p4, p5, p6, p7, p8, p9 (+1 more)"


def test_paths_brief_has_no_suffix_at_the_detail_boundary() -> None:
    assert diff._paths_brief([f"p{index}" for index in range(10)]) == "p0, p1, p2, p3, p4, p5, p6, p7, p8, p9"


def test_build_estimate_accepts_one_worker(tmp_path: Path) -> None:
    costs_file = tmp_path / "coverage.substrate.costs.json"
    pytest_costs.write_costs(costs_file, {"a_test.py::test_a": 10.0, "b_test.py::test_b": 20.0})

    assert diff.build_estimate(costs_file, 1) == 30.0


def test_build_estimate_is_none_without_costs(tmp_path: Path) -> None:
    assert diff.build_estimate(tmp_path / "missing.costs.json", 4) is None


def test_build_banner_rounds_up_to_at_least_one_minute() -> None:
    assert "~1 min" in diff._build_banner("missing_coverage", 60.0)


def test_build_banner_divides_the_estimate_by_sixty_minutes() -> None:
    assert "~100 min" in diff._build_banner("missing_coverage", 6000.0)


# --- report_build / report_wall ----------------------------------------------------------------------


def test_report_timings_round_to_six_decimals() -> None:
    state = diff.RunState(timings={"preflight": 0.1234567})
    report = diff.report_build(state, diff.RunStatus.CLEAN)

    timings = report["timings"]
    assert isinstance(timings, dict)
    assert timings["preflight"] == 0.123457


def test_report_wall_seconds_returns_zero_without_a_timings_map() -> None:
    assert diff.report_wall_seconds({}) == 0.0


# --- report_write ------------------------------------------------------------------------------------


def test_report_write_creates_nested_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "report.json"

    diff.report_write(path, {"status": "CLEAN"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "CLEAN"}


def test_report_write_keeps_unicode_indent_and_sorted_keys(tmp_path: Path) -> None:
    path = tmp_path / "report.json"

    diff.report_write(path, {"z": "café", "a": 1})

    text = path.read_text(encoding="utf-8")
    assert '"café"' in text
    assert '\n  "a": 1' in text
    assert text.index('"a"') < text.index('"z"')
    assert text.endswith("\n")


def test_report_write_syncs_the_stream_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fsync_calls: list[int] = []
    close_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", fsync_calls.append)
    monkeypatch.setattr(os, "close", close_calls.append)

    diff.report_write(tmp_path / "report.json", {"status": "CLEAN"})

    assert len(fsync_calls) == 2
    assert len(close_calls) == 1


def test_report_write_flushes_before_syncing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fdopen = os.fdopen
    flushes: list[str] = []

    def spy_fdopen(
        fd: int,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
    ):
        stream = real_fdopen(fd, mode, buffering, encoding, errors, newline, closefd)

        class _Spy:
            def __init__(self) -> None:
                self._stream = stream

            def write(self, data: str) -> int:
                return self._stream.write(data)

            def flush(self) -> None:
                flushes.append("flush")
                self._stream.flush()

            def fileno(self) -> int:
                return self._stream.fileno()

            def __enter__(self) -> Self:
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                self._stream.__exit__(exc_type, exc_value, traceback)

        return _Spy()

    monkeypatch.setattr(os, "fdopen", spy_fdopen)

    diff.report_write(tmp_path / "report.json", {"status": "CLEAN"})

    assert "flush" in flushes


# --- stats / validation ------------------------------------------------------------------------------


def test_stats_apply_merges_the_campaign_timings() -> None:
    state = diff.RunState()
    stats = _campaign_stats(
        enumerated=1,
        status_counts={"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
        timings={"mutation": 5.0},
    )

    diff._stats_apply(state, stats)

    assert state.timings["mutation"] == 5.0
    assert state.counts["enumerated"] == 1


def test_raw_validate_rejects_a_single_status_mismatch() -> None:
    mutant = diff_gate.parse_mutants(json.dumps([raw_mutant("survived")]))[0]
    stats = _campaign_stats(
        enumerated=1, status_counts={"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0}
    )

    with pytest.raises(ValueError, match="status counts"):
        diff._raw_validate([mutant], stats)


# --- substrate integrity -----------------------------------------------------------------------------


def test_substrate_integrity_issue_reports_missing_metadata(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")

    assert diff.substrate_integrity_issue(coverage) == "missing_metadata"


def test_substrate_integrity_issue_reports_missing_coverage(tmp_path: Path) -> None:
    assert diff.substrate_integrity_issue(coverage_path(tmp_path)) == "missing_coverage"


def test_substrate_integrity_issue_reports_a_coverage_checksum_mismatch(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text(
        json.dumps({"coverage_sha256": "wrong", "costs_sha256": "whatever"}), encoding="utf-8"
    )

    assert diff.substrate_integrity_issue(coverage) == "coverage_checksum"


def test_substrate_integrity_issue_reports_invalid_metadata_when_unparseable(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text("{not json", encoding="utf-8")

    assert diff.substrate_integrity_issue(coverage) == "invalid_metadata"


def test_substrate_integrity_issue_reports_missing_costs(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text(
        json.dumps({"coverage_sha256": substrate.file_sha256(coverage)}), encoding="utf-8"
    )

    assert diff.substrate_integrity_issue(coverage) == "missing_costs"


def test_substrate_integrity_issue_reports_a_costs_checksum_mismatch(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    costs = substrate.costs_path(coverage)
    costs.write_text("{}", encoding="utf-8")
    substrate.metadata_path(coverage).write_text(
        json.dumps({"coverage_sha256": substrate.file_sha256(coverage), "costs_sha256": "wrong"}), encoding="utf-8"
    )

    assert diff.substrate_integrity_issue(coverage) == "costs_checksum"


def test_substrate_integrity_issue_accepts_a_matching_costs_checksum(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    costs = substrate.costs_path(coverage)
    costs.write_text("{}", encoding="utf-8")
    substrate.metadata_path(coverage).write_text(
        json.dumps({"coverage_sha256": substrate.file_sha256(coverage), "costs_sha256": substrate.file_sha256(costs)}),
        encoding="utf-8",
    )

    assert diff.substrate_integrity_issue(coverage) == "invalid_metadata"


def test_substrate_integrity_issue_reports_invalid_metadata_for_a_non_dict(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text("[1, 2]", encoding="utf-8")

    assert diff.substrate_integrity_issue(coverage) == "invalid_metadata"


# --- status exit code --------------------------------------------------------------------------------


def test_status_exit_codes() -> None:
    assert diff.status_exit_code(diff.RunStatus.CLEAN) == 0
    assert diff.status_exit_code(diff.RunStatus.FINDINGS) == 1
    assert diff.status_exit_code(diff.RunStatus.UNRESOLVED) == 1
    assert diff.status_exit_code(diff.RunStatus.SCOPE_CHANGED) == 3
    assert diff.status_exit_code(diff.RunStatus.BASELINE_FAILURE) == 2
    assert diff.status_exit_code(diff.RunStatus.INFRA_FAILURE) == 2


# --- _rendered_findings ------------------------------------------------------------------------------


def test_rendered_findings_returns_empty_for_non_list() -> None:
    assert diff._rendered_findings("nope") == []


def test_rendered_findings_defaults_a_missing_reason_to_empty() -> None:
    findings: diff.JsonValue = [{"status": "killed", "path": "src/x.py", "line": 1, "operator": "op"}]

    assert diff._rendered_findings(findings) == [("killed", "src/x.py", 1, "op", "")]


def test_rendered_findings_skips_non_dict_entries() -> None:
    findings: diff.JsonValue = [
        {"status": "survived", "path": "src/x.py", "line": 1, "operator": "op", "reason": ""},
        "not-a-dict",
        {"status": "no_coverage", "path": "src/y.py", "line": 2, "operator": "op2", "reason": ""},
    ]

    assert len(diff._rendered_findings(findings)) == 2


def test_rendered_findings_requires_every_field_typed() -> None:
    findings: diff.JsonValue = [{"status": "survived", "path": 123, "line": 1, "operator": "op", "reason": ""}]

    assert diff._rendered_findings(findings) == []


# --- render_human ------------------------------------------------------------------------------------


def _failure_report(
    *, baseline_failure_cached: bool = False, baseline_failure_tests: list[str] | None = None
) -> diff.JsonObject:
    state = diff.RunState(
        baseline_failure_cached=baseline_failure_cached,
        baseline_failure_tests=baseline_failure_tests if baseline_failure_tests is not None else [],
    )
    return diff.failure_report(
        state,
        diff.RunStatus.BASELINE_FAILURE,
        stage=diff.FailureStage.SUBSTRATE,
        code="baseline_failed",
        next_action="fix",
    )


def test_render_lists_the_detail_line() -> None:
    report = diff.report_build(diff.RunState(error_detail="something broke"), diff.RunStatus.INFRA_FAILURE)

    assert "detail: something broke" in diff.render_human(report)


def test_render_skips_scope_drift_when_empty() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.CLEAN)
    report["scope_drift"] = []

    assert "scope-drift:" not in diff.render_human(report)


def test_render_skips_an_empty_detail() -> None:
    report = diff.report_build(diff.RunState(error_detail=""), diff.RunStatus.INFRA_FAILURE)

    assert "detail:" not in diff.render_human(report)


def test_render_skips_an_empty_next_action() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.INFRA_FAILURE)
    report["next_action"] = ""

    assert "next:" not in diff.render_human(report)


def test_render_skips_time_when_one_stage_is_missing() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.CLEAN)
    report["timings"] = {"substrate": 5, "mutation": None}

    assert "time:" not in diff.render_human(report)


def test_render_skips_the_miss_reason_when_empty() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.CLEAN)
    report["substrate"] = {"miss_reason": ""}

    assert "substrate-miss:" not in diff.render_human(report)


def test_render_skips_baseline_with_no_tests() -> None:
    report = _failure_report(baseline_failure_cached=True)

    assert "baseline-cache" not in diff.render_human(report)


def test_render_lists_first_ten_baseline_failures_and_counts_the_rest() -> None:
    tests = [f"tests/t{index}_test.py::test_x" for index in range(11)]

    rendered = diff.render_human(_failure_report(baseline_failure_cached=True, baseline_failure_tests=tests))

    assert "BASELINE tests/t10_test.py::test_x" not in rendered
    assert "BASELINE ... 1 more" in rendered


def test_render_lists_no_more_baseline_at_exactly_ten() -> None:
    tests = [f"tests/t{index}_test.py::test_x" for index in range(10)]

    rendered = diff.render_human(_failure_report(baseline_failure_cached=True, baseline_failure_tests=tests))

    assert "BASELINE ..." not in rendered


def test_render_sorts_the_counts_object() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.CLEAN)

    rendered = diff.render_human(report)
    match = re.search(r"counts: (\{.*\})", rendered)
    assert match is not None
    assert match.group(1).index('"cache_resolved"') < match.group(1).index('"enumerated"')


# --- boundary wrappers -------------------------------------------------------------------------------


def test_command_run_without_stdout_file_returns_the_returncode(tmp_path: Path) -> None:
    argv = (sys.executable, "-c", "import sys; sys.exit(3)")

    assert diff._command_run(diff.CommandInvocation(argv=argv), cwd=tmp_path) == 3


def test_command_run_with_a_stdout_file_captures_output(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    argv = (sys.executable, "-c", "print('hello')")

    code = diff._command_run(diff.CommandInvocation(argv=argv, stdout_path=out), cwd=tmp_path)

    assert code == 0
    assert out.read_text(encoding="utf-8") == "hello\n"


def test_command_run_with_a_stdout_file_returns_a_failure_code(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    argv = (sys.executable, "-c", "import sys; sys.exit(2)")

    assert diff._command_run(diff.CommandInvocation(argv=argv, stdout_path=out), cwd=tmp_path) == 2


def test_command_run_forwards_cwd_env_text_and_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: _RunCapture = {"argv": [], "kwargs": {"cwd": Path(), "env": {}, "text": False, "stdout": sys.stderr}}

    class _Completed:
        returncode = 0

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], stdout: TextIO, text: bool, check: bool
    ) -> _Completed:
        captured["argv"] = argv
        captured["kwargs"] = {"cwd": cwd, "env": env, "stdout": stdout, "text": text}
        return _Completed()

    monkeypatch.setattr(diff.subprocess, "run", fake_run)

    invocation = diff.CommandInvocation(argv=("echo", "x"), environment={"A": "1"})
    assert diff._command_run(invocation, cwd=tmp_path) == 0

    assert captured["argv"] == ["echo", "x"]
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["env"]["A"] == "1"
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["stdout"] is sys.stderr


def test_command_run_with_a_stdout_file_forwards_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: _RunCapture = {"argv": [], "kwargs": {"cwd": Path(), "env": {}, "text": False, "stdout": sys.stderr}}

    class _Completed:
        returncode = 0

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], stdout: TextIO, text: bool, check: bool
    ) -> _Completed:
        captured["kwargs"] = {"cwd": cwd, "env": env, "stdout": stdout, "text": text}
        return _Completed()

    monkeypatch.setattr(diff.subprocess, "run", fake_run)

    out = tmp_path / "out.txt"
    invocation = diff.CommandInvocation(argv=("echo", "x"), environment={"A": "1"}, stdout_path=out)
    assert diff._command_run(invocation, cwd=tmp_path) == 0

    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["env"]["A"] == "1"
    assert captured["kwargs"]["text"] is True
    stdout = captured["kwargs"]["stdout"]
    assert isinstance(stdout, TextIOWrapper)
    assert stdout.name == str(out)


def test_substrate_ensure_forwards_the_force_rebuild_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_ensure(
        coverage_path: Path,
        expected: substrate.SubstrateMetadata,
        build: substrate.SubstrateBuilder,
        force_rebuild: bool,
        tree: substrate.TreeSnapshotter,
        unmeasured: substrate.UnmeasuredFiles,
    ) -> str:
        captured["coverage_path"] = coverage_path
        captured["expected"] = expected
        captured["force_rebuild"] = force_rebuild
        return "result"

    monkeypatch.setattr(substrate, "substrate_ensure", fake_ensure)

    profile = project_profile(Path.cwd())
    meta = expected(profile.root)
    cov_path = coverage_path(profile.root)
    result = diff._substrate_ensure(
        cov_path,
        meta,
        build=lambda path: 0,
        build_mode=diff.SubstrateBuildMode.REBUILD,
        tree=lambda: substrate.TreeRecord(hashes={}, reexports={}),
        unmeasured=frozenset,
    )

    assert result == "result"
    assert captured["coverage_path"] == cov_path
    assert captured["expected"] == meta
    assert captured["force_rebuild"] is True


def test_substrate_seed_returns_none_without_a_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_pool_dir(root: Path) -> None:
        return None

    monkeypatch.setattr(pool, "pool_dir", no_pool_dir)
    called: list[object] = []

    def no_pool_seed(*args: object, **kwargs: object) -> None:
        called.append(args)

    monkeypatch.setattr(pool, "pool_seed", no_pool_seed)
    profile = project_profile(tmp_path)

    result = diff._substrate_seed(
        profile.layout,
        tmp_path,
        coverage_path(tmp_path),
        expected(tmp_path),
        substrate.TreeRecord(hashes={}, reexports={}),
        frozenset(),
    )

    assert result is None
    assert called == []


def test_substrate_seed_delegates_to_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "pool"

    def shared_pool_dir(root: Path) -> Path:
        return shared

    monkeypatch.setattr(pool, "pool_dir", shared_pool_dir)
    seen: dict[str, object] = {}

    def fake_seed(*args: object, **kwargs: object) -> str:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "seeded"

    monkeypatch.setattr(pool, "pool_seed", fake_seed)
    profile = project_profile(tmp_path)
    cov_path = coverage_path(tmp_path)
    meta = expected(tmp_path)
    current = substrate.TreeRecord(hashes={}, reexports={})

    result = diff._substrate_seed(
        profile.layout,
        tmp_path,
        cov_path,
        meta,
        current,
        frozenset(),
    )

    assert result == "seeded"
    assert seen["args"] == (shared, cov_path, meta, current)
    assert seen["kwargs"] == {"in_diff": frozenset(), "layout": profile.layout}


def test_substrate_share_publishes_to_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "pool"

    def shared_pool_dir(root: Path) -> Path:
        return shared

    monkeypatch.setattr(pool, "pool_dir", shared_pool_dir)
    published: list[object] = []

    def record_publish(*args: object) -> None:
        published.append(args)

    monkeypatch.setattr(pool, "pool_publish", record_publish)

    cov_path = coverage_path(tmp_path)
    meta = expected(tmp_path)
    diff._substrate_share(tmp_path, cov_path, meta)

    assert published == [(shared, cov_path, meta)]


def test_substrate_share_skips_when_there_is_no_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_pool_dir(root: Path) -> None:
        return None

    monkeypatch.setattr(pool, "pool_dir", no_pool_dir)
    published: list[object] = []

    def record_publish(*args: object) -> None:
        published.append(args)

    monkeypatch.setattr(pool, "pool_publish", record_publish)

    diff._substrate_share(tmp_path, coverage_path(tmp_path), expected(tmp_path))

    assert published == []


def test_load_report_sinks_appends_each_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Point:
        name = "sink"

        def load(self) -> object:
            return "loaded-object"

    def _entry_points(group: str) -> list[_Point]:
        return [_Point()]

    monkeypatch.setattr(diff, "entry_points", _entry_points)

    assert diff.load_report_sinks() == [("sink", "loaded-object")]


def test_runtime_dependencies_wires_the_real_boundaries(tmp_path: Path) -> None:
    profile = project_profile(tmp_path)
    isolation = NoIsolation()

    runtime = diff.runtime_dependencies(profile, isolation)

    assert isinstance(runtime, diff.DiffRuntime)
    assert runtime.clock is time.monotonic
    assert runtime.report_sink is diff.entry_point_sink
    assert runtime.substrate_inspect is substrate.substrate_inspect
    assert runtime.scope_write is diff_scope.write_scope
    measure = runtime.measure
    assert isinstance(measure, partial)
    assert measure.keywords["isolation"] is isolation
    scope_resolve = runtime.scope_resolve
    assert isinstance(scope_resolve, partial)
    assert scope_resolve.args == (profile.root,)
    substrate_seed = runtime.substrate_seed
    assert isinstance(substrate_seed, partial)
    assert substrate_seed.args == (profile.layout,)


def test_main_wires_runtime_dependencies_with_profile_then_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` hands ``runtime_dependencies`` the profile first and the isolation second."""
    write_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_dependencies(profile: Profile, isolation: Isolation) -> diff.DiffRuntime:
        captured["profile"] = profile
        captured["isolation"] = isolation
        raise RuntimeError("stop before running")

    monkeypatch.setattr(diff, "runtime_dependencies", fake_dependencies)

    with pytest.raises(RuntimeError):
        diff.main([], root=tmp_path)

    seen_profile = captured["profile"]
    assert isinstance(seen_profile, Profile)
    assert seen_profile.layout == project_profile(tmp_path).layout
    assert isinstance(captured["isolation"], NoIsolation)


def test_scope_resolve_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()

    def fake_resolve(base_ref: str, repo_root: Path, base_mode: diff_scope.BaseMode) -> object:
        return sentinel

    monkeypatch.setattr(diff_scope, "resolve_scope", fake_resolve)

    assert diff._scope_resolve(Path(), "ref", diff_scope.BaseMode.MERGE_BASE) is sentinel


def test_scope_changes_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()

    def fake_changes(scope: diff_scope.DiffScope, repo_root: Path) -> object:
        return sentinel

    monkeypatch.setattr(diff_scope, "scope_changes", fake_changes)

    assert diff._scope_changes(scope(), Path()) is sentinel


# --- main / _load ------------------------------------------------------------------------------------


def test_invalid_profile_writes_an_error_and_returns_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "missing.py"\n')

    return_code = diff.main([], root=tmp_path)

    assert return_code == 2
    assert "invalid [tool.balamut]" in capsys.readouterr().err


def test_doctor_invalid_profile_returns_two(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "missing.py"\n')

    assert diff.doctor_main([], root=tmp_path) == 2


def test_main_uses_the_real_environ_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path)
    captured: dict[str, object] = {}

    def fake_isolation_for(profile: object, environ: object) -> NoIsolation:
        captured["environ"] = environ
        return NoIsolation()

    monkeypatch.setattr(diff, "isolation_for", fake_isolation_for)

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert captured["environ"] is os.environ


def test_doctor_uses_the_real_environ_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Scenario(tmp_path)
    captured: dict[str, object] = {}

    def fake_isolation_for(profile: object, environ: object) -> NoIsolation:
        captured["environ"] = environ
        return NoIsolation()

    monkeypatch.setattr(diff, "isolation_for", fake_isolation_for)

    diff.doctor_main([], runtime=None, root=tmp_path)

    assert captured["environ"] is os.environ


def test_orchestration_error_writes_a_stderr_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path)

    def failing_snapshot(layout: Layout) -> substrate.TreeRecord:
        return (_ for _ in ()).throw(OSError("boom"))

    monkeypatch.setattr(substrate, "tree_snapshot", failing_snapshot)

    return_code = diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert return_code != 0
    assert "the orchestration boundary failed: boom" in capsys.readouterr().err


# --- the orchestrator's error branches ---------------------------------------------------------------


def test_unreachable_isolation_writes_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, probe_error=IsolationError("connection refused"))

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "the isolation target is unreachable" in capsys.readouterr().err


def test_scope_resolve_error_writes_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, scope_error=OSError("git unavailable"))

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "resolving the scope failed: git unavailable" in capsys.readouterr().err


def test_preflight_command_error_writes_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, preflight_error=OSError("no such command"))

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "preflight check-environment: no such command" in capsys.readouterr().err


def test_mutation_boundary_error_writes_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, mutation_error=OSError("mutator unavailable"))

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "the mutation boundary failed: mutator unavailable" in capsys.readouterr().err


def test_invalid_artifacts_write_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, raw_text="not-json")

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "the mutation artifacts are invalid" in capsys.readouterr().err


def test_baseline_failure_writes_a_stderr_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=1, coverage_failed_nodeids=(_FAILED,))

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "the baseline failed" in capsys.readouterr().err


def test_report_write_failure_writes_a_stderr_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path, preflight_returncode=1)
    actual = diff.report_write
    attempts = 0

    def fail_once(path: Path, report: diff.JsonMapping) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk full")
        actual(path, report)

    monkeypatch.setattr(diff, "report_write", fail_once)

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "publishing the report failed: disk full" in capsys.readouterr().err


# --- the orchestrator's boundary branches ------------------------------------------------------------


def test_main_creates_the_state_directory(tmp_path: Path) -> None:
    run_main(Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")]))

    assert (tmp_path / ".balamut" / ".gitignore").is_file()


def test_run_creates_the_raw_json_directory(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")])

    run_main(scenario, "--json-out", str(tmp_path / "a" / "b" / "raw.json"))

    assert (tmp_path / "a" / "b" / "raw.json").is_file()


def test_gate_publishes_the_raw_mutation_json(tmp_path: Path) -> None:
    run_main(Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")]))

    assert (tmp_path / "raw.json").is_file()


def test_scope_write_error_is_an_infrastructure_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path)

    def failing_write(pinned: diff_scope.DiffScope, path: Path) -> None:
        raise OSError("disk full")

    runtime = replace(scenario.runtime(), scope_write=failing_write)

    return_code = diff.main(main_args(tmp_path), runtime=runtime, root=tmp_path)

    assert return_code != 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "scope_write_error"
    assert "writing the scope failed: disk full" in capsys.readouterr().err


def test_confirmation_infra_failure_is_reported(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, focused_coverage_returncode=1)
    _record(tmp_path, (_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert report["error_code"] == "baseline_confirmation_infra_failure"
    # An empty measure detail must read as ``None``, not the empty string: ``detail or None`` collapses it.
    assert report["error_detail"] is None


def test_substrate_infrastructure_error_is_reported(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)

    def failing_ensure(*args: object, **kwargs: object) -> substrate.SubstrateMetadata:
        raise OSError("disk full")

    runtime = replace(scenario.runtime(), substrate_ensure=failing_ensure)

    return_code = diff.main(main_args(tmp_path), runtime=runtime, root=tmp_path)

    assert return_code != 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "substrate_error"


def test_raw_validate_rejects_a_count_mismatch_with_the_mutants(tmp_path: Path) -> None:
    stats: Row = {
        "schema_version": 1,
        "complete": True,
        "enumerated": 1,
        "cache_resolved": 0,
        "executed": 1,
        "terminal": 1,
        "status_counts": {"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
    }
    rows = [raw_mutant("killed", bucket=""), raw_mutant("killed", key="k2", bucket="")]
    scenario = Scenario(tmp_path, rows=rows, stats_payload=stats)

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert report["error_code"] == "artifact_invalid"


# --- timing ------------------------------------------------------------------------------------------


def test_a_clean_run_records_each_stage_timing(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")], clock_fn=_big_step_clock())

    _return_code, report = run_main(scenario)

    timings = section(report, "timings")
    assert timings["substrate"] == 1000.0
    assert timings["mutation"] == 1000.0
    assert timings["gate"] == 1000.0


def test_a_failed_confirmation_records_the_substrate_timing(tmp_path: Path) -> None:
    scenario = Scenario(
        tmp_path,
        coverage_returncode=1,
        focused_coverage_returncode=1,
        coverage_failed_nodeids=(_FAILED,),
        clock_fn=_big_step_clock(),
    )
    _record(tmp_path, (_FAILED,))

    _return_code, report = run_main(scenario)

    assert section(report, "timings")["substrate"] == 1000.0


def test_a_mutation_error_records_the_mutation_timing(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, mutation_error=OSError("mutator unavailable"), clock_fn=_big_step_clock())

    _return_code, report = run_main(scenario)

    assert section(report, "timings")["mutation"] == 1000.0


def test_invalid_artifacts_record_the_gate_timing(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, raw_text="not-json", clock_fn=_big_step_clock())

    _return_code, report = run_main(scenario)

    assert section(report, "timings")["gate"] == 1000.0


# --- _announce_drift ---------------------------------------------------------------------------------


def _make_run(tmp_path: Path, scenario: Scenario) -> diff._Run:
    profile = project_profile(tmp_path)
    options = diff.RunOptions(
        base="origin/main",
        base_mode=diff_scope.BaseMode.MERGE_BASE,
        mode=diff.DiffMode.FAST,
        coverage_path=coverage_path(tmp_path),
        raw_json_path=tmp_path / "raw.json",
        report_json_path=tmp_path / "report.json",
        agent_json=False,
        rebuild_substrate=False,
        mutate_args=(),
    )
    return diff._Run(options, profile, NoIsolation(), scenario.runtime(), {}, diff.RunState())


def test_mutation_reports_a_post_campaign_scope_change(tmp_path: Path) -> None:
    """The mutation stage re-checks the scope after the campaign: a stale scope returns the scope-changed report."""
    changes = diff_scope.ScopeChanges(current=False, paths=("src/x.py",), moved_refs=())
    scenario = Scenario(tmp_path, rows=[raw_mutant("survived")], scope_changes=changes)
    run = _make_run(tmp_path, scenario)
    run.state.scope = scope()
    temporary = tmp_path / "temporary"
    temporary.mkdir()

    report = run._mutation(temporary, scope(), fresh_generation=None)

    assert report["status"] == diff.RunStatus.SCOPE_CHANGED.value
    assert report["next_action"] == "restart_with_fresh_scope"
    assert report["findings"] == []


def test_announce_drift_names_new_source_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _make_run(tmp_path, Scenario(tmp_path))
    drift = substrate.TreeDrift(reason="tree_new_src", paths=(), new_src=("src/new.py",))

    run._announce_drift(drift, drift_rebuild=True)

    assert "source files the build tree does not know, rebuilding: src/new.py" in capsys.readouterr().err


def test_announce_drift_names_diverged_files_and_counts_only_the_refreshable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _make_run(tmp_path, Scenario(tmp_path))
    drift = substrate.TreeDrift(reason="tree_changed_outside_diff", paths=("src/a.py", "src/b.py"))

    run._announce_drift(drift, drift_rebuild=False)

    assert "2 files outside the diff diverged from the build tree" in capsys.readouterr().err
    assert run.state.substrate_drift_files == 2


def test_announce_drift_counts_zero_when_rebuilding(tmp_path: Path) -> None:
    run = _make_run(tmp_path, Scenario(tmp_path))
    drift = substrate.TreeDrift(reason="tree_changed_outside_diff", paths=("src/a.py",))

    run._announce_drift(drift, drift_rebuild=True)

    assert run.state.substrate_drift_files == 0


def test_announce_drift_names_test_support(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _make_run(tmp_path, Scenario(tmp_path))
    drift = substrate.TreeDrift(
        reason="tree_support_changed", paths=(), support=("conftest.py",), support_tests=("tests/x_test.py",)
    )

    run._announce_drift(drift, drift_rebuild=False)

    assert "test support diverged from the build tree" in capsys.readouterr().err


# --- _doctor -----------------------------------------------------------------------------------------


def test_doctor_stops_at_the_first_failed_preflight(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        "from balamut.config import init\n\ninit(preflight=(('first',), ('second',)))\n",
    )
    scenario = Scenario(tmp_path)

    def failing_command(invocation: diff.CommandInvocation) -> int:
        return 1

    runtime = replace(scenario.runtime(), command_run=failing_command)

    diff.doctor_main(["--coverage", str(coverage_path(tmp_path))], runtime=runtime, root=tmp_path)

    issues = json.loads(capsys.readouterr().out)["issues"]
    assert issues.count("preflight_failed") == 1


def test_doctor_output_is_sorted_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path)

    diff.doctor_main(["--coverage", str(coverage_path(tmp_path))], runtime=scenario.runtime(), root=tmp_path)

    out = capsys.readouterr().out
    assert out.index('"issues"') < out.index('"status"')


# --- _publish ----------------------------------------------------------------------------------------


def test_publish_double_failure_writes_the_report_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path, preflight_returncode=1)

    def failing_write(path: Path, report: diff.JsonMapping) -> None:
        (_ for _ in ()).throw(OSError("persistent"))

    monkeypatch.setattr(diff, "report_write", failing_write)

    return_code = diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    captured = capsys.readouterr()
    assert return_code == diff.status_exit_code(diff.RunStatus.INFRA_FAILURE)
    assert "publishing the report failed again: persistent" in captured.err
    assert json.loads(captured.out)["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert scenario.reports[-1]["status"] == diff.RunStatus.INFRA_FAILURE.value
    # The stdout fallback is sorted JSON: ``sort_keys=True`` puts baseline_failure before status.
    assert captured.out.index('"baseline_failure"') < captured.out.index('"status"')


def test_agent_json_keeps_unicode_unescaped_and_sorted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, preflight_error=OSError("café"))

    diff.main(main_args(tmp_path, "--agent-json"), runtime=scenario.runtime(), root=tmp_path)

    out = capsys.readouterr().out
    assert "café" in out
    assert out.index('"counts"') < out.index('"status"')


# --- the failure cache removal paths -----------------------------------------------------------------


def test_rebuild_substrate_removes_a_stale_failure_cache(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    _record(tmp_path, (_FAILED,))

    run_main(scenario, "--rebuild-substrate")

    assert _read(tmp_path) is None


def test_passing_confirmation_removes_the_cache_even_if_the_build_fails(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, focused_coverage_returncode=0)
    _record(tmp_path, (_FAILED,))

    def failing_ensure(*args: object, **kwargs: object) -> substrate.SubstrateMetadata:
        raise OSError("disk full")

    runtime = replace(scenario.runtime(), substrate_ensure=failing_ensure)

    diff.main(main_args(tmp_path), runtime=runtime, root=tmp_path)

    assert _read(tmp_path) is None


def test_infra_build_failure_removes_a_stale_cache(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=1)
    baseline_failure.failure_write(
        coverage_path(tmp_path),
        baseline_failure.BaselineFailureRecord("0" * 64, ("tests/stale.py::test_stale",)),
    )

    run_main(scenario)

    assert not baseline_failure.failure_path(coverage_path(tmp_path)).exists()


def test_failed_confirmation_rewrites_the_cache_with_fresh_failures(tmp_path: Path) -> None:
    scenario = Scenario(
        tmp_path,
        coverage_returncode=1,
        focused_coverage_returncode=1,
        coverage_failed_nodeids=("tests/x_test.py::test_new_failure",),
    )
    _record(tmp_path, (_FAILED,))

    run_main(scenario)

    record = _read(tmp_path)
    assert record is not None
    assert record.failed_nodeids == ("tests/x_test.py::test_new_failure",)


# --- more timing branches ----------------------------------------------------------------------------


def test_a_failed_build_records_the_substrate_timing(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=1, coverage_failed_nodeids=(_FAILED,), clock_fn=_big_step_clock())

    _return_code, report = run_main(scenario)

    assert section(report, "timings")["substrate"] == 1000.0


def test_confirmation_infra_failure_records_the_substrate_timing(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, focused_coverage_returncode=1, clock_fn=_big_step_clock())
    _record(tmp_path, (_FAILED,))

    _return_code, report = run_main(scenario)

    assert section(report, "timings")["substrate"] == 1000.0


def test_substrate_error_records_timing_and_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, clock_fn=_big_step_clock())

    def failing_ensure(*args: object, **kwargs: object) -> substrate.SubstrateMetadata:
        raise OSError("disk full")

    runtime = replace(scenario.runtime(), substrate_ensure=failing_ensure)

    return_code = diff.main(main_args(tmp_path), runtime=runtime, root=tmp_path)

    assert return_code != 0
    assert "the substrate failed: disk full" in capsys.readouterr().err
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["timings"]["substrate"] == 1000.0


# --- the coverage-build detail message ---------------------------------------------------------------


def test_coverage_build_requires_a_detail_for_the_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=2)

    diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    assert "coverage build failed" not in capsys.readouterr().err


def test_coverage_build_failure_message_with_a_detail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=2)
    real_measure = scenario.measure

    def measure_with_detail(request: MeasureRequest) -> MeasureResult:
        return replace(real_measure(request), detail="boom")

    runtime = replace(scenario.runtime(), measure=measure_with_detail)

    diff.main(main_args(tmp_path), runtime=runtime, root=tmp_path)

    assert "the coverage build failed: boom" in capsys.readouterr().err


def test_doctor_invalid_substrate_returns_two(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)

    return_code = diff.doctor_main(
        ["--coverage", str(coverage_path(tmp_path))], runtime=scenario.runtime(), root=tmp_path
    )

    assert return_code == 2
