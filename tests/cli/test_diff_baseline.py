"""A red baseline in ``balamut diff``: the failure cache, focused confirmation, red builds, refresh failures and
scope changes found during the run."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from balamut.cli import diff, mutate
from balamut.coverage import baseline_failure, substrate
from balamut.diff.scope import DiffScope, ScopeChanges
from balamut.isolation import NoIsolation
from bm_support import write_file
from bm_support.diff import (
    Report,
    Scenario,
    coverage_path,
    expected,
    mutation_argv,
    published_metadata,
    raw_mutant,
    run_main,
)
from bm_support.scopes import scope

_FAILED = "tests/x_test.py::test_regression"


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
        baseline_failure.BaselineFailureRecord(
            fingerprint=_fingerprint(root), failed_nodeids=nodeids, generation=generation
        ),
    )


def _read(root: Path) -> baseline_failure.BaselineFailureRecord | None:
    return baseline_failure.failure_read(coverage_path(root), expected_fingerprint=_fingerprint(root))


def test_render_lists_attributed_baseline_failures() -> None:
    state = diff.RunState(baseline_failure_cached=True, baseline_failure_tests=["tests/x_test.py::test_regression"])
    report = diff.failure_report(
        state,
        diff.RunStatus.BASELINE_FAILURE,
        stage=diff.FailureStage.SUBSTRATE,
        code="baseline_failed",
        next_action="fix",
    )

    rendered = diff.render_human(report)

    assert "baseline-cache: confirmed" in rendered
    assert "BASELINE tests/x_test.py::test_regression" in rendered


def test_render_announces_the_cache_a_failed_build_records() -> None:
    state = diff.RunState(baseline_failure_tests=["tests/x_test.py::test_regression"])
    report = diff.failure_report(
        state,
        diff.RunStatus.BASELINE_FAILURE,
        stage=diff.FailureStage.SUBSTRATE,
        code="baseline_failed",
        next_action="fix",
    )

    assert "baseline-cache: recorded" in diff.render_human(report)


def test_failed_full_baseline_publishes_an_exact_failure_cache(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, coverage_returncode=1, coverage_failed_nodeids=(_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.BASELINE_FAILURE.value
    assert report["baseline_failure"] == {"cached": False, "tests": [_FAILED]}
    assert _read(tmp_path) == baseline_failure.BaselineFailureRecord(_fingerprint(tmp_path), (_FAILED,))


def test_failed_collection_path_is_cached_and_confirmed(tmp_path: Path) -> None:
    nodeid = "tests/broken_test.py"
    scenario = Scenario(
        tmp_path, coverage_returncode=1, focused_coverage_returncode=1, coverage_failed_nodeids=(nodeid,)
    )

    first, first_report = run_main(scenario)
    second, second_report = run_main(scenario)

    assert first != 0
    assert second != 0
    assert first_report["status"] == diff.RunStatus.BASELINE_FAILURE.value
    assert second_report["baseline_failure"] == {"cached": True, "tests": [nodeid]}
    assert scenario.measures[-1].targets == (nodeid,)


def test_a_matching_cache_needs_a_fresh_focused_failure_before_the_fast_exit(tmp_path: Path) -> None:
    scenario = Scenario(
        tmp_path, coverage_returncode=1, focused_coverage_returncode=1, coverage_failed_nodeids=(_FAILED,)
    )
    _record(tmp_path, (_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["baseline_failure"] == {"cached": True, "tests": [_FAILED]}
    assert scenario.events.count("coverage-confirm") == 1
    assert "coverage-build" not in scenario.events
    assert "substrate-ensure" not in scenario.events
    assert "mutation" not in scenario.events
    assert scenario.measures[0].targets == ("tests/x_test.py",)


def test_confirmation_runs_the_files_the_fingerprint_pins(tmp_path: Path) -> None:
    """The fingerprint holds the set of test files, not the functions in them: a test removed or renamed by the fix
    would fail a node-id confirmation as not found on every later run."""
    failed = ("tests/b_test.py::TestCase::test_z", "tests/a_test.py::test_x[case a]", "tests/a_test.py::test_y")
    scenario = Scenario(tmp_path, focused_coverage_returncode=0)
    _record(tmp_path, failed)

    run_main(scenario)

    assert scenario.measures[0].targets == ("tests/a_test.py", "tests/b_test.py")


def test_a_matching_cache_is_confirmed_over_a_compatible_substrate(tmp_path: Path) -> None:
    Scenario(tmp_path)
    cached = published_metadata(expected(tmp_path), generation="cached-generation")
    scenario = Scenario(
        tmp_path, focused_coverage_returncode=1, coverage_failed_nodeids=(_FAILED,), cached_metadata=cached
    )
    _record(tmp_path, (_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["baseline_failure"] == {"cached": True, "tests": [_FAILED]}
    assert "coverage-confirm" in scenario.events
    assert "substrate-ensure" not in scenario.events


def test_a_repeated_failed_confirmation_keeps_the_published_generation(tmp_path: Path) -> None:
    Scenario(tmp_path)
    generation = "published-red-generation"
    cached = published_metadata(expected(tmp_path), generation=generation)
    scenario = Scenario(
        tmp_path, focused_coverage_returncode=1, coverage_failed_nodeids=(_FAILED,), cached_metadata=cached
    )
    _record(tmp_path, (_FAILED,), generation=generation)

    first, _first_report = run_main(scenario)

    assert first != 0
    assert _read(tmp_path) == baseline_failure.BaselineFailureRecord(_fingerprint(tmp_path), (_FAILED,), generation)

    scenario.focused_coverage_returncode = 0
    second, second_report = run_main(scenario)

    assert second == 0
    assert second_report["status"] == diff.RunStatus.CLEAN.value
    assert scenario.ensure_force is False
    assert "coverage-build" not in scenario.events


def test_a_passing_confirmation_rebuilds_the_full_substrate(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, focused_coverage_returncode=0)
    _record(tmp_path, (_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert report["status"] == diff.RunStatus.CLEAN.value
    assert scenario.events.index("coverage-confirm") < scenario.events.index("coverage-build")
    assert _read(tmp_path) is None


def test_a_passing_confirmation_rebuilds_a_compatible_substrate(tmp_path: Path) -> None:
    Scenario(tmp_path)
    cached = published_metadata(expected(tmp_path), generation="cached-generation")
    scenario = Scenario(tmp_path, focused_coverage_returncode=0, cached_metadata=cached)
    _record(tmp_path, (_FAILED,))

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert scenario.events.index("coverage-confirm") < scenario.events.index("coverage-build")
    assert scenario.ensure_force is True
    substrate_report = report["substrate"]
    assert isinstance(substrate_report, dict)
    assert substrate_report["miss_reason"] == "baseline_confirmation_passed"


def test_a_mismatched_cache_is_ignored_and_replaced_by_a_full_build(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    baseline_failure.failure_write(
        coverage_path(tmp_path), baseline_failure.BaselineFailureRecord("0" * 64, ("tests/stale_test.py::test_stale",))
    )

    return_code, _report = run_main(scenario)

    assert return_code == 0
    assert "coverage-confirm" not in scenario.events
    assert "coverage-build" in scenario.events


def test_rebuild_substrate_bypasses_a_matching_cache(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    _record(tmp_path, ("tests/cached_test.py::test_cached",))

    return_code, _report = run_main(scenario, "--rebuild-substrate")

    assert return_code == 0
    assert "coverage-confirm" not in scenario.events
    assert "coverage-build" in scenario.events
    assert scenario.ensure_force is True


def _suite(root: Path) -> None:
    write_file(root / "tests/x_test.py", "def test_regression():\n    assert True\n")


def test_a_red_build_publishes_its_coverage_and_the_record_names_it(tmp_path: Path) -> None:
    _suite(tmp_path)
    scenario = Scenario(tmp_path, coverage_returncode=1, coverage_failed_nodeids=(_FAILED,), real_substrate=True)

    _return_code, report = run_main(scenario)

    assert report["status"] == diff.RunStatus.BASELINE_FAILURE.value
    assert report["baseline_failure"] == {"cached": False, "tests": [_FAILED]}
    published = substrate.substrate_inspect(coverage_path(tmp_path))
    assert published is not None
    substrate_report = report["substrate"]
    assert isinstance(substrate_report, dict)
    assert substrate_report["generation"] == published.generation
    record = _read(tmp_path)
    assert record is not None
    assert record.generation == published.generation


def test_a_fixed_baseline_reuses_the_red_build_and_leaves_the_failed_file_to_refresh(tmp_path: Path) -> None:
    _suite(tmp_path)
    scenario = Scenario(
        tmp_path,
        coverage_returncode=1,
        coverage_failed_nodeids=(_FAILED,),
        focused_coverage_returncode=0,
        real_substrate=True,
    )
    _red_code, red = run_main(scenario)

    _fixed_code, fixed = run_main(scenario)

    assert red["status"] == diff.RunStatus.BASELINE_FAILURE.value
    assert fixed["status"] == diff.RunStatus.CLEAN.value
    assert scenario.events.count("coverage-build") == 1, "fixing the baseline does not rebuild the whole suite"
    assert scenario.events.count("coverage-confirm") == 1
    red_substrate, fixed_substrate = red["substrate"], fixed["substrate"]
    assert isinstance(red_substrate, dict)
    assert isinstance(fixed_substrate, dict)
    assert fixed_substrate["generation"] == red_substrate["generation"]
    assert fixed_substrate["reused"] is True
    assert fixed_substrate["drift_files"] == 1
    assert _read(tmp_path) is None


def test_an_in_run_build_vouches_for_scope_freshness(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)

    return_code, _report = run_main(scenario)

    assert return_code == 0
    argv = mutation_argv(scenario)
    assert argv[argv.index("--fresh-substrate-generation") + 1] == "rebuilt-generation"


def test_a_forced_rebuild_vouches_for_scope_freshness(tmp_path: Path) -> None:
    Scenario(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=published_metadata(expected(tmp_path), generation="old-generation"))

    run_main(scenario, "--rebuild-substrate")

    argv = mutation_argv(scenario)
    assert argv[argv.index("--fresh-substrate-generation") + 1] == "rebuilt-generation"


def test_a_reused_substrate_does_not_vouch_for_scope_freshness(tmp_path: Path) -> None:
    Scenario(tmp_path)
    scenario = Scenario(
        tmp_path, cached_metadata=published_metadata(expected(tmp_path), generation="cached-generation")
    )

    run_main(scenario)

    assert "--fresh-substrate-generation" not in mutation_argv(scenario)


def test_a_generation_published_by_another_run_does_not_vouch(tmp_path: Path) -> None:
    """A neighbour's build may have started before this scope was pinned: it proves nothing about its files."""
    scenario = Scenario(tmp_path, foreign_generation="foreign-generation")

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert "coverage-build" not in scenario.events
    assert "--fresh-substrate-generation" not in mutation_argv(scenario)
    substrate_report = report["substrate"]
    assert isinstance(substrate_report, dict)
    assert substrate_report["generation"] == "foreign-generation"


def test_a_refresh_failure_names_the_failing_tests_without_forcing_a_rebuild(tmp_path: Path) -> None:
    failing = ("tests/x_test.py::test_x", "tests/x_test.py::test_y[param]")
    scenario = Scenario(tmp_path, refresh_failed_nodeids=failing)

    return_code, report = run_main(scenario)

    assert return_code == 2
    assert report["status"] == diff.RunStatus.BASELINE_FAILURE.value
    assert (report["failure_stage"], report["error_code"]) == ("coverage_refresh", "baseline_failed")
    assert report["baseline_failure"] == {"cached": False, "tests": list(failing)}
    assert report["next_action"] == "fix_baseline_tests_and_rerun"
    assert not baseline_failure.failure_path(coverage_path(tmp_path)).exists()
    assert not (tmp_path / "raw.json").exists()


def test_a_refresh_failure_does_not_claim_a_recorded_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_main(Scenario(tmp_path, refresh_failed_nodeids=("tests/x_test.py::test_x",)))

    rendered = capsys.readouterr().out
    assert "BASELINE tests/x_test.py::test_x" in rendered
    assert "baseline-cache" not in rendered


def test_a_refresh_failure_exit_without_named_tests_is_an_infrastructure_failure(tmp_path: Path) -> None:
    return_code, report = run_main(Scenario(tmp_path, mutation_returncode=mutate.EXIT_REFRESH_BASELINE_FAILURE))

    assert return_code == 2
    assert (report["failure_stage"], report["error_code"]) == ("coverage_refresh", "refresh_failure_artifact_missing")


def test_the_test_inventory_ignores_test_body_edits(tmp_path: Path) -> None:
    test_file = write_file(tmp_path / "tests/example_test.py", "def test_example():\n    assert True\n")
    initial = diff.inventory_fingerprint(diff.load_profile(tmp_path).layout)

    test_file.write_text("def test_example():\n    assert False\n\ndef test_new():\n    assert 1\n", encoding="utf-8")

    assert diff.inventory_fingerprint(diff.load_profile(tmp_path).layout) == initial


@pytest.mark.parametrize("change", ["add", "remove", "rename"])
def test_the_test_inventory_tracks_the_set_of_collectable_files(tmp_path: Path, change: str) -> None:
    tests_dir = tmp_path / "tests"
    write_file(tests_dir / "example_test.py", "def test_example():\n    assert True\n")
    initial = diff.inventory_fingerprint(diff.load_profile(tmp_path).layout)

    if change == "add":
        write_file(tests_dir / "added_test.py", "def test_added():\n    assert True\n")
    elif change == "remove":
        (tests_dir / "example_test.py").unlink()
    else:
        (tests_dir / "example_test.py").rename(tests_dir / "renamed_test.py")

    assert diff.inventory_fingerprint(diff.load_profile(tmp_path).layout) != initial


def test_the_test_inventory_ignores_non_collectable_files(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/example_test.py", "def test_example():\n    assert True\n")
    initial = diff.inventory_fingerprint(diff.load_profile(tmp_path).layout)

    write_file(tmp_path / "tests/helpers.py", "VALUE = 1\n")

    assert diff.inventory_fingerprint(diff.load_profile(tmp_path).layout) == initial


def test_a_content_change_names_the_changed_paths_and_moved_refs(tmp_path: Path) -> None:
    changes = ScopeChanges(current=False, paths=("src/x.py", "tests/x_test.py"), moved_refs=("head",))

    return_code, report = run_main(Scenario(tmp_path, rows=[raw_mutant("survived")], scope_changes=changes))

    assert return_code == 3
    assert report["error_detail"] == "changed during run: src/x.py, tests/x_test.py"
    assert report["scope_drift"] == ["head"]


def test_the_scope_detail_names_the_first_paths_and_counts_the_rest(tmp_path: Path) -> None:
    paths = tuple(f"src/module_{index:02d}.py" for index in range(13))
    changes = ScopeChanges(current=False, paths=paths, moved_refs=())

    _return_code, report = run_main(Scenario(tmp_path, rows=[raw_mutant("survived")], scope_changes=changes))

    assert report["error_detail"] == f"changed during run: {', '.join(paths[:10])} (+3 more)"


def test_an_unreadable_scope_state_is_scope_changed_without_detail(tmp_path: Path) -> None:
    changes = ScopeChanges(current=False, paths=(), moved_refs=())

    _return_code, report = run_main(Scenario(tmp_path, rows=[raw_mutant("survived")], scope_changes=changes))

    assert report["status"] == diff.RunStatus.SCOPE_CHANGED.value
    assert report["error_detail"] == "scope state unreadable"


def test_moved_refs_without_a_content_change_keep_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shared `git fetch` moving the base ref is no reason to throw a run away: the diff is pinned to its base."""
    changes = ScopeChanges(current=True, paths=(), moved_refs=("base_ref",))

    return_code, report = run_main(Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")], scope_changes=changes))

    assert return_code == 0
    assert report["scope_drift"] == ["base_ref"]
    assert (
        "scope-drift: base_ref moved during run; verdict is for pinned diff base diff-base-sha"
        in capsys.readouterr().out
    )


def _with_scope_checks(scenario: Scenario, checks: list[ScopeChanges]) -> tuple[int, Report]:
    answers: Iterator[ScopeChanges] = iter(checks)

    def scope_changes(_scope: DiffScope, _root: Path) -> ScopeChanges:
        scenario.events.append("scope-current")
        return next(answers)

    return run_main(scenario, runtime=replace(scenario.runtime(), scope_changes=scope_changes))


_CURRENT = ScopeChanges(current=True, paths=(), moved_refs=())
_EDITED = ScopeChanges(current=False, paths=("src/x.py",), moved_refs=("head",))


def test_a_scope_edit_during_the_build_stops_before_the_campaign(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, rows=[raw_mutant("survived")])

    return_code, report = _with_scope_checks(scenario, [_EDITED])

    assert return_code == 3
    assert report["error_detail"] == "changed during run: src/x.py"
    assert report["scope_drift"] == ["head"]
    assert "coverage-build" in scenario.events, "the substrate is published and serves the next run"
    assert "mutation" not in scenario.events


@pytest.mark.parametrize(
    ("final_check", "detail"),
    [
        pytest.param(_EDITED, "changed during run: src/x.py", id="edited"),
        pytest.param(_CURRENT, "changed during run: content restored before the final check", id="restored"),
    ],
)
def test_an_engine_reported_scope_change_is_named_by_the_orchestrator_check(
    tmp_path: Path, final_check: ScopeChanges, detail: str
) -> None:
    scenario = Scenario(tmp_path, mutation_returncode=mutate.EXIT_SCOPE_CHANGED)

    return_code, report = _with_scope_checks(scenario, [_CURRENT, final_check])

    assert return_code == 3
    assert report["status"] == diff.RunStatus.SCOPE_CHANGED.value
    assert report["error_detail"] == detail
    assert report["findings"] == []
