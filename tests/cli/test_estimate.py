"""The campaign duration estimator (balamut.cli.estimate): tiered work, daemons, and the file breakdown."""

from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path
from typing import TypedDict

import pytest
from coverage import CoverageData

from balamut.cli import estimate
from balamut.config import load_profile
from balamut.coverage import costs as pytest_costs
from balamut.coverage.coverage_map import CoverageMap
from balamut.paths import substrate_path
from bm_support import make_layout, make_project, write_file
from bm_support.modules import reloaded


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


class _Captured(TypedDict):
    argv: list[str]
    kwargs: dict[str, object]
    cache_exists_at_run: bool


class _Probed(TypedDict):
    file: str
    args: tuple[dict[str, list[tuple[str, ...]]], dict[str, float], float, argparse.Namespace]


# Kept ahead of the module reload in test_module_constants_have_their_documented_values, which recreates every
# function of the module.


def test_main_defaults_the_coverage_to_the_profile_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    monkeypatch.chdir(root)

    assert estimate.main([]) == 2

    expected = substrate_path(load_profile(root))
    assert capsys.readouterr().err == f"error: no coverage substrate at {expected}: run `balamut diff` first\n"


def test_main_reads_an_absolute_measured_path_relative_to_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    data = CoverageData(basename=str(coverage))
    data.set_context("tests/test_calc.py::test_clamp_low|run")
    data.add_lines({str(root.resolve() / "src/pkg/calc.py"): {2, 3}})
    data.write()
    monkeypatch.chdir(root)

    assert estimate.main(["src/pkg/calc.py", "--coverage", str(coverage), "--daemons", "2"]) == 0

    out = capsys.readouterr().out
    assert "none covered" not in out
    assert "=== estimate: src/pkg/calc.py ===" in out


def test_main_with_no_covered_mutants_prints_no_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj", {"src/pkg/only.py": "def f(x: int) -> bool:\n    return x < 0\n"})
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    monkeypatch.chdir(root)

    def empty_coverage_map(*_args: object, **_kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    monkeypatch.setattr(estimate, "load_coverage_map", empty_coverage_map)

    assert estimate.main(["src/pkg/only.py", "--coverage", str(coverage)]) == 0

    assert capsys.readouterr().out == "5 mutants, none covered: nothing to run\n"


def test_main_probe_costs_an_unmeasured_test_at_the_median(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file whose only test has no measured duration costs the median (0.2), less than the measured 0.4."""
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"measured": 0.4, "low": 0.1, "mid": 0.2})
    monkeypatch.chdir(root)

    def no_coverage_map(*_args: object, **_kwargs: object) -> None:
        return None

    def covered_two(*_args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 2, {"measured.py": [("measured",)], "unmeasured.py": [("unmeasured",)]}, {"measured", "unmeasured"}

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)
    monkeypatch.setattr(estimate, "_covered", covered_two)
    probed: list[str] = []

    def fake_probe(file: str, *_args: object) -> float:
        probed.append(file)
        return 0.6

    monkeypatch.setattr(estimate, "_probe_overhead", fake_probe)

    code = estimate.main(["--coverage", str(coverage), "--costs", str(costs), "--probe", "1", "--daemons", "2"])

    assert code == 0
    assert probed == ["unmeasured.py"]


def test_probe_overhead_is_none_without_a_request_timing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Completed:
        stderr = "campaign final: done=0\n"

    def fake_run(_argv: list[str], **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr(estimate.subprocess, "run", fake_run)
    args = argparse.Namespace(coverage="cov", probe=1, daemons=2, workers=1)

    assert estimate._probe_overhead("untimed.py", {"untimed.py": [("t1",)]}, {"t1": 0.1}, 0.1, args) is None
    assert capsys.readouterr().err == ""


def test_module_constants_have_their_documented_values() -> None:
    # The module-level constants execute only at import; a reload inside the test body re-runs them so the
    # coverage substrate attributes the lines to this test (a COLD mutant then reads as unsupported, not a gap).
    with reloaded(estimate):
        assert estimate._OVERHEAD_FALLBACK_S == 0.35
        assert estimate._EXPENSIVE_S == 0.5
        assert estimate._KILLER_FIRST_EXPENSIVE_SHARE == 0.5
        assert estimate._STARTUP_FALLBACK_S == 3.0


def test_work_of_tiers_cheap_and_expensive() -> None:
    costs = {"a": 0.4, "b": 0.6, "c": 1.0}  # a is cheap (< 0.5), b and c expensive
    # cross-kill: cheap 0.4 / 1 + expensive (0.6 + 1.0) / min(2, 2) = 0.8 -> 1.2
    assert _close(estimate.work_of(("a", "b", "c"), costs, 0.1, 2), 1.2)
    assert _close(estimate.work_of(("a",), costs, 0.1, 2), 0.4)
    assert _close(estimate.work_of(("unknown",), costs, 0.1, 2), 0.1)  # the median stands in for an unknown cost
    # killer-first: the cheap tier always, half of the expensive one (skipped when a cheap test kills)
    assert _close(estimate.work_of(("a", "b", "c"), costs, 0.1, 2, expensive_survive=0.5), 0.8)


def test_work_of_tiers_exactly_at_the_half_second_boundary() -> None:
    # A test costing exactly 0.5 is cheap by the strict `<` split (and expensive by `>=`): a mixed pair makes the
    # parallel division differ between the two tierings.
    costs = {"cheap": 0.4, "boundary": 0.5}
    assert _close(estimate.work_of(("cheap", "boundary"), costs, 0.1, 2), 0.9)


def test_work_of_splits_two_cheap_tests_across_workers() -> None:
    # Two cheap tests over two workers: their total divides by two slots, not one.
    assert _close(estimate.work_of(("a", "b"), {"a": 0.4, "b": 0.3}, 0.1, 2), 0.35)


def test_work_of_zero_workers_falls_back_to_one_slot() -> None:
    # workers=0 collapses min(...) to 0 and the `or 1` fallback keeps the denominator at one slot.
    assert _close(estimate.work_of(("a",), {"a": 0.4}, 0.1, 0), 0.4)
    assert _close(estimate.work_of(("b",), {"b": 0.6}, 0.1, 0), 0.6)


def test_estimate_run_is_the_sum_of_per_mutant_cost_over_daemons() -> None:
    costs = {"t1": 0.2, "t2": 0.6}
    by_file = {"a.py": [("t1",), ("t1", "t2")], "b.py": [("t2",)]}
    # per mutant with 0.5 overhead: 0.7, 1.3, 1.1 -> 3.1 over 2 daemons = 1.55
    run, rows = estimate.estimate_run(by_file, costs, 0.2, 0.5, daemons=2, workers=2)

    assert _close(run, 1.55)
    assert {row[1] for row in rows} == {"a.py", "b.py"}
    killer_first, _rows = estimate.estimate_run(by_file, costs, 0.2, 0.5, daemons=2, workers=2, expensive_survive=0.5)
    assert killer_first < run


def test_estimate_scales_inversely_with_daemons() -> None:
    by_file = {"a.py": [("t",)] * 12}
    six, _rows = estimate.estimate_run(by_file, {"t": 0.5}, 0.5, 0.5, daemons=6, workers=2)
    twelve, _rows = estimate.estimate_run(by_file, {"t": 0.5}, 0.5, 0.5, daemons=12, workers=2)

    assert _close(six, 2 * twelve)


def test_estimate_run_sorts_rows_by_cost_descending() -> None:
    by_file = {"cheap.py": [("t1",)], "dear.py": [("t2",)]}
    _run, rows = estimate.estimate_run(by_file, {"t1": 0.1, "t2": 2.0}, 0.5, 0.5, daemons=1, workers=1)

    assert [row[1] for row in rows] == ["dear.py", "cheap.py"]


def test_estimate_run_averages_per_mutant_cost() -> None:
    # A file with two coverings reports its average as total-cost / mutant-count, not total * count.
    by_file = {"a.py": [("t1",), ("t2",)]}
    _run, rows = estimate.estimate_run(by_file, {"t1": 0.2, "t2": 0.6}, 0.5, 0.35, daemons=1, workers=1)

    (run_s, name, count, average) = rows[0]
    assert name == "a.py"
    assert count == 2
    assert _close(average, 0.75)  # (0.35+0.2 + 0.35+0.6) / 2
    assert _close(run_s, 1.5)


def test_the_formula_matches_a_recorded_run_within_five_percent() -> None:
    """A guard of the formula on fixed inputs recorded from a real campaign (6 daemons x 2 workers, 135 s wall, a
    probed overhead of 0.562 s): dividing by slots instead of daemons, or losing the overhead, breaks it."""
    covered, total_work, overhead, daemons, workers, startup, actual = 1160, 92.0, 0.562, 6, 2, 5.0, 135.0
    cost = total_work / covered
    run, _rows = estimate.estimate_run(
        {"src/pkg/recorded.py": [("t",)] * covered}, {"t": cost}, cost, overhead, daemons=daemons, workers=workers
    )

    assert abs(startup + run - actual) / actual <= 0.05


def test_very_fast_floor_of_no_rows_is_zero() -> None:
    assert _close(estimate.very_fast_floor([]), 0.0)


def test_select_files_of_no_rows_is_empty() -> None:
    assert estimate.select_files([], top=0) == []


def test_select_files_ranks_by_average_cost_not_total(tmp_path: Path) -> None:
    """The breakdown ranks by average seconds per mutant (index 3), not by total run seconds."""
    rows = [
        (100.0, "many.py", 100, 1.0),
        (10.0, "few.py", 1, 10.0),
    ]

    assert [row[1] for row in estimate.select_files(rows, top=2)] == ["few.py", "many.py"]


def test_select_files_keeps_the_slow_and_collapses_the_very_fast() -> None:
    rows = [
        (10.0, "slow.py", 5, 3.0),
        (8.0, "mid.py", 10, 0.8),
        (5.0, "fast1.py", 50, 0.40),
        (4.0, "fast2.py", 40, 0.39),
        (3.0, "fast3.py", 30, 0.38),
    ]

    assert _close(estimate.very_fast_floor(rows), 0.57)
    shown = {row[1] for row in estimate.select_files(rows, top=0)}
    assert {"slow.py", "mid.py"} <= shown
    assert len(shown) == 4
    assert "fast3.py" not in shown


def test_select_files_keeps_rows_exactly_at_the_floor() -> None:
    rows = [
        (10.0, "slow.py", 5, 3.0),
        (6.0, "bound.py", 50, 0.4 * 1.5),  # exactly the very-fast floor (0.4 * 1.5)
        (5.0, "mid1.py", 50, 0.5),
        (4.0, "mid2.py", 40, 0.45),
        (3.0, "fast.py", 30, 0.4),
    ]

    shown = {row[1] for row in estimate.select_files(rows, top=0)}
    assert "bound.py" in shown
    assert shown == {"slow.py", "bound.py", "mid1.py", "mid2.py"}


def test_select_files_top_override_is_exact() -> None:
    rows = [(10.0, "a", 5, 3.0), (8.0, "b", 10, 0.8), (3.0, "c", 30, 0.38)]

    assert len(estimate.select_files(rows, top=2)) == 2
    assert [row[1] for row in estimate.select_files(rows, top=1)] == ["a"]


def test_covered_counts_mutants_and_unions_covering_tests(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "proj")
    write_file(layout.root / "src/pkg/a.py", "def f(x: int) -> bool:\n    return x < 0\n")
    write_file(layout.root / "src/pkg/b.py", "def g(x: int) -> bool:\n    return x < 0\n")
    cov = CoverageMap(
        by_file={
            "src/pkg/a.py": {2: frozenset({"tests/test_a.py::test_f"})},
            "src/pkg/b.py": {2: frozenset({"tests/test_b.py::test_g"})},
        }
    )

    total, by_file, union = estimate._covered(["src/pkg/a.py", "src/pkg/b.py"], layout, cov)

    assert total == 10  # five src mutants per single-comparison function
    assert by_file == {
        "src/pkg/a.py": [("tests/test_a.py::test_f",)] * 5,
        "src/pkg/b.py": [("tests/test_b.py::test_g",)] * 5,
    }
    assert union == {"tests/test_a.py::test_f", "tests/test_b.py::test_g"}


def test_probe_overhead_measures_request_minus_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file = "a.py"
    cache = Path(tempfile.gettempdir()) / f"balamut-estimate-{abs(hash(file))}.sqlite"
    cache.write_text("stale", encoding="utf-8")

    captured: _Captured = {"argv": [], "kwargs": {}, "cache_exists_at_run": True}

    class Completed:
        stderr = "request (fork, patch, tests, bus) 1000ms"

    def fake_run(argv: list[str], **kwargs: object) -> Completed:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        captured["cache_exists_at_run"] = cache.exists()
        cache.write_text("created-by-campaign", encoding="utf-8")  # as the real subprocess would
        return Completed()

    monkeypatch.setattr(estimate.subprocess, "run", fake_run)
    monkeypatch.setattr(estimate, "resolve_workers", lambda: 99)

    args = argparse.Namespace(coverage="cov", probe=2, daemons=2, workers=1)
    by_file = {"a.py": [("t1",), ("t2",)]}
    costs = {"t1": 0.1, "t2": 0.2}

    overhead = estimate._probe_overhead(file, by_file, costs, 0.1, args)

    assert overhead is not None
    assert _close(overhead, 0.85)  # 1000ms/1000 - (0.1 + 0.2)/2
    assert captured["cache_exists_at_run"] is False  # the stale cache was unlinked before the campaign
    assert not cache.exists()  # and after it
    argv = captured["argv"]
    assert argv[argv.index("--daemons") + 1] == "2"
    assert argv[argv.index("--limit") + 1] == "2"
    kwargs = captured["kwargs"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 600
    assert kwargs["check"] is False
    assert "probe: a.py: overhead 850ms per mutant" in capsys.readouterr().err


def test_probe_overhead_tolerates_a_missing_cache_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = "b.py"
    cache = Path(tempfile.gettempdir()) / f"balamut-estimate-{abs(hash(file))}.sqlite"
    cache.unlink(missing_ok=True)  # no stale cache from a previous run

    class Completed:
        stderr = "request (fork, patch, tests, bus) 1000ms"

    def fake_run(argv: list[str], **kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr(estimate.subprocess, "run", fake_run)
    monkeypatch.setattr(estimate, "resolve_workers", lambda: 99)

    args = argparse.Namespace(coverage="cov", probe=1, daemons=2, workers=1)
    by_file = {"b.py": [("t1",)]}
    costs = {"t1": 0.1}

    overhead = estimate._probe_overhead(file, by_file, costs, 0.1, args)

    assert overhead is not None
    assert _close(overhead, 0.9)  # 1000ms/1000 - 0.1


def test_main_with_invalid_profile_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nunknown = "key"\n')
    monkeypatch.chdir(tmp_path)

    code = estimate.main([])

    assert code == 2
    assert "invalid [tool.balamut]" in capsys.readouterr().err


def test_main_without_coverage_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert estimate.main(["--coverage", str(tmp_path / "missing")]) == 2
    assert "no coverage substrate" in capsys.readouterr().err


def test_main_with_no_covered_mutants_reports_nothing_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")  # a valid substrate with nothing measured
    pytest_costs.write_costs(root / "coverage.costs.json", {})
    monkeypatch.chdir(root)

    def empty_coverage_map(*args: object, **kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    monkeypatch.setattr(estimate, "load_coverage_map", empty_coverage_map)

    code = estimate.main(["src/pkg/calc.py", "--coverage", str(coverage), "--costs", str(root / "coverage.costs.json")])

    assert code == 0
    assert "none covered" in capsys.readouterr().out


def test_main_estimates_from_the_substrate_coverage_and_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    data = CoverageData(basename=str(coverage))
    data.set_context("tests/test_calc.py::test_clamp_low|run")
    data.add_lines({"src/pkg/calc.py": {2, 3}})
    data.write()
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"tests/test_calc.py::test_clamp_low": 0.2})
    monkeypatch.chdir(root)

    code = estimate.main(["src/pkg/calc.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "2"])

    out = capsys.readouterr().out
    assert code == 0
    assert "=== estimate: src/pkg/calc.py ===" in out
    assert "killer-first" in out
    assert "cross-kill" in out
    assert "src/pkg/calc.py" in out


def test_main_reads_costs_from_the_sidecar_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    data = CoverageData(basename=str(coverage))
    data.set_context("tests/test_calc.py::test_clamp_low|run")
    data.add_lines({"src/pkg/calc.py": {2, 3}})
    data.write()
    pytest_costs.write_costs(root / "coverage.costs.json", {"tests/test_calc.py::test_clamp_low": 0.2})
    monkeypatch.chdir(root)

    code = estimate.main(["src/pkg/calc.py", "--coverage", str(coverage), "--daemons", "2"])

    out = capsys.readouterr().out
    assert code == 0
    assert "measured duration 100%" in out
    assert "2 x 1 daemons" in out  # --workers defaults to 1


def test_main_defaults_median_when_no_test_has_a_measured_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    data = CoverageData(basename=str(coverage))
    data.set_context("tests/test_calc.py::test_clamp_low|run")
    data.add_lines({"src/pkg/calc.py": {2, 3}})
    data.write()
    monkeypatch.chdir(root)

    code = estimate.main(["src/pkg/calc.py", "--coverage", str(coverage), "--daemons", "2", "--workers", "1"])

    out = capsys.readouterr().out
    assert code == 0
    assert "0.45s/mutant" in out  # 0.35 overhead + 0.1 median stand-in


def test_main_without_probe_uses_the_constant_overhead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    data = CoverageData(basename=str(coverage))
    data.set_context("tests/test_calc.py::test_clamp_low|run")
    data.add_lines({"src/pkg/calc.py": {2, 3}})
    data.write()
    pytest_costs.write_costs(root / "coverage.costs.json", {"tests/test_calc.py::test_clamp_low": 0.2})
    monkeypatch.chdir(root)

    calls: list[tuple[object, ...]] = []

    def constant_probe(*args: object) -> float:
        calls.append(args)
        return 0.5

    monkeypatch.setattr(estimate, "_probe_overhead", constant_probe)

    code = estimate.main(
        ["src/pkg/calc.py", "--coverage", str(coverage), "--costs", str(root / "coverage.costs.json"), "--daemons", "2"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "a constant" in out
    assert calls == []


def test_main_probe_selects_the_cheapest_file_and_marks_it_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 0.1, "t2": 0.9})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_two(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"a.py": [("t1",)], "b.py": [("t2",)]}, {"t1", "t2"}

    monkeypatch.setattr(estimate, "_covered", covered_two)

    probed: _Probed = {"file": "", "args": ({}, {}, 0.0, argparse.Namespace())}

    def fake_probe(
        file: str,
        by_file: dict[str, list[tuple[str, ...]]],
        probe_costs: dict[str, float],
        median: float,
        args: argparse.Namespace,
    ) -> float:
        probed["file"] = file
        probed["args"] = (by_file, probe_costs, median, args)
        return 0.6

    monkeypatch.setattr(estimate, "_probe_overhead", fake_probe)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--probe", "1", "--daemons", "2", "--workers", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert probed["file"] == "a.py"  # cheaper: work 0.1 vs 0.9
    by_file, probe_costs, median, args = probed["args"]
    assert by_file == {"a.py": [("t1",)], "b.py": [("t2",)]}
    assert probe_costs == {"t1": 0.1, "t2": 0.9}
    assert median == 0.5
    assert isinstance(args, argparse.Namespace)
    assert "probed" in out
    assert "600ms per mutant" in out


def test_main_probe_picks_the_cheapest_file_not_the_first_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe targets the file with the least covering work, not the alphabetically first name."""
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"cheap": 0.1, "dear": 0.9})
    monkeypatch.chdir(root)

    def no_coverage_map(*_a: object, **_k: object) -> None:
        return None

    def covered_two(*_a: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"z.py": [("cheap",)], "a.py": [("dear",)]}, {"cheap", "dear"}

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)
    monkeypatch.setattr(estimate, "_covered", covered_two)

    probed: dict[str, object] = {}

    def fake_probe(file: str, *args: object) -> float:
        probed["file"] = file
        return 0.6

    monkeypatch.setattr(estimate, "_probe_overhead", fake_probe)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--probe", "1", "--daemons", "2", "--workers", "1"]
    )

    assert code == 0
    assert probed["file"] == "z.py"


def test_main_renders_exact_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 20000.0})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)
    monkeypatch.setattr(estimate, "resolve_workers", lambda: 3)

    def covered_one(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"src/a.py": [("t1",)]}, {"t1"}

    monkeypatch.setattr(estimate, "_covered", covered_one)

    code = estimate.main(["src/a.py", "--coverage", str(coverage), "--costs", str(costs), "--workers", "1"])

    out = capsys.readouterr().out
    assert code == 0
    assert "3 x 1 daemons" in out  # the resolve_workers fallback
    assert "~56 min (3336s)" in out  # killer-first: (3 + (0.35 + 0.5*20000)/3)
    assert "~111 min (6670s)" in out  # cross-kill: (3 + (0.35 + 20000)/3)
    assert "mutants 10 (covered 1, no coverage 9)" in out
    assert "350ms per mutant" in out
    assert "measured duration 100%" in out


def test_main_breakdown_line_and_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 1.0, "t2": 0.5, "t3": 0.2})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_three(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"a.py": [("t1",)], "b.py": [("t2",)], "c.py": [("t3",)]}, {"t1", "t2", "t3"}

    monkeypatch.setattr(estimate, "_covered", covered_three)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "1", "--workers", "1", "--top", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "s/mutant" in out
    assert "49%" in out  # a.py share = 1.35 / 2.75
    assert "1s        +2 very fast files" in out  # rest = cross_kill - shown
    assert "+2 very fast files" in out
    assert "measured duration 100%" in out


def test_main_hides_no_very_fast_files_when_everything_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 1.0, "t2": 0.2})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_two(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"a.py": [("t1",)], "b.py": [("t2",)]}, {"t1", "t2"}

    monkeypatch.setattr(estimate, "_covered", covered_two)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "1", "--workers", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "very fast files" not in out


def test_main_measured_share_is_the_intersection_over_the_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 0.2})  # only t1 is measured; t2 is not
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_two(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"a.py": [("t1",)], "b.py": [("t2",)]}, {"t1", "t2"}

    monkeypatch.setattr(estimate, "_covered", covered_two)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "1", "--workers", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "measured duration 50%" in out


def test_main_defaults_measured_share_to_full_when_the_union_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 0.2})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_empty_union(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"a.py": [("t1",)]}, set()

    # covered > 0 but an empty covering-test union: the measured share falls back to 100%.
    monkeypatch.setattr(estimate, "_covered", covered_empty_union)

    code = estimate.main(
        ["a.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "1", "--workers", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "measured duration 100%" in out


def test_main_share_falls_back_to_zero_when_cross_kill_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "proj")
    coverage = root / "coverage"
    coverage.write_text("x", encoding="utf-8")
    costs = root / "coverage.costs.json"
    pytest_costs.write_costs(costs, {"t1": 0.2})
    monkeypatch.chdir(root)

    def no_coverage_map(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(estimate, "load_coverage_map", no_coverage_map)

    def covered_one(*args: object) -> tuple[int, dict[str, list[tuple[str, ...]]], set[str]]:
        return 10, {"zzz.py": [("t1",)]}, {"t1"}

    monkeypatch.setattr(estimate, "_covered", covered_one)

    def zero_run(*args: object, **kwargs: object) -> tuple[float, list[tuple[float, str, int, float]]]:
        return 0.0, [(0.0, "zzz.py", 1, 0.0)]

    monkeypatch.setattr(estimate, "estimate_run", zero_run)

    code = estimate.main(
        ["zzz.py", "--coverage", str(coverage), "--costs", str(costs), "--daemons", "1", "--workers", "1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "0s   0%  zzz.py" in out  # the share guard: cross_kill == 0 -> a 0% share, not a division by zero
