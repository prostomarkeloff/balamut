"""The ``balamut mutate`` entry point: guards, diff routing, invalidation, stats and the scope watch.

Every test runs in its own project directory; ``run_campaign`` is replaced wherever a campaign would start, so no
daemon is spawned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast

import pytest
from coverage import CoverageData

from balamut.cli import mutate, substrate_cmd
from balamut.config import load_kind_overrides, load_profile
from balamut.core.mutants import Mutant
from balamut.coverage import costs as pytest_costs
from balamut.coverage import failures, substrate
from balamut.coverage.coverage_map import CoverageMap
from balamut.coverage.refresh import MeasureContext, RefreshBaselineError
from balamut.coverage.store import Store, StoreCoverage
from balamut.diff.scope import DiffScope, ScopeWatch, write_scope
from balamut.engine import campaign, collection, stats
from balamut.engine.enumeration import known_kinds, resolve_kinds
from balamut.engine.results import CampaignScopeChangedError, MutantResult, MutantStatus
from balamut.isolation import isolation_for
from balamut.operators.src import enumerate_mutants
from balamut.paths import default_address
from balamut.report.agent import MutantRecord
from balamut.report.html import read_baseline
from bm_support import make_layout, write_file
from bm_support.scopes import pinned, scope
from bm_support.substrates import expected_substrate_metadata, write_valid_coverage

type Kwargs = dict[str, object]


class _EnsureCapture(TypedDict):
    args: tuple[object, ...]
    kwargs: dict[str, object]


@pytest.fixture(autouse=True)
def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean project directory and a process environment the entry point may edit."""
    monkeypatch.chdir(tmp_path)
    for name in ("PYTEST_ADDOPTS", "PYTEST_FAST_MARK", "PYTEST_FAST_ENV_PREFIXES", "PYTEST_FAST_WATCH_DIRS"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("BALAMUT_TEST_ROOTS", raising=False)


def _argv(tmp_path: Path, *, kind: str, paths: list[str]) -> list[str]:
    return [
        *paths,
        "--kind",
        kind,
        "--coverage",
        str(tmp_path / "nope.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--address",
        str(tmp_path / "unused.sock"),
    ]


@dataclass(slots=True)
class _CampaignCall:
    mutants: list[Mutant] = field(default_factory=list[Mutant])
    initial_costs: object = None
    scope_watch: ScopeWatch | None = None
    options: campaign.CampaignOptions | None = None
    calls: int = 0


def _recording_campaign(call: _CampaignCall) -> Callable[..., list[MutantResult]]:
    def capture(mutants: list[Mutant], _sources: object, _cov: object, **kwargs: object) -> list[MutantResult]:
        call.calls += 1
        call.mutants.extend(mutants)
        call.initial_costs = kwargs.get("initial_costs")
        watch = kwargs.get("scope_watch")
        call.scope_watch = watch if isinstance(watch, ScopeWatch) else None
        opts = kwargs.get("options")
        call.options = opts if isinstance(opts, campaign.CampaignOptions) else None
        return []

    return capture


def _collected(*nodeids: str) -> Callable[..., frozenset[str]]:
    def collect(_files: frozenset[str], **_kwargs: object) -> frozenset[str]:
        return frozenset(nodeids)

    return collect


def _changed(*paths: str) -> Callable[..., list[str]]:
    def prepare(*_args: object, **_kwargs: object) -> list[str]:
        return list(paths)

    return prepare


def test_pure_oracle_does_not_require_coverage_file(tmp_path: Path) -> None:
    assert mutate.main(_argv(tmp_path, kind="oracle", paths=[])) == 0


@pytest.mark.parametrize("kind", ["src", "oracle,src"])
def test_without_the_refresh_a_source_kind_requires_a_coverage_file(tmp_path: Path, kind: str) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    assert mutate.main([*_argv(tmp_path, kind=kind, paths=["src/x.py"]), "--no-refresh-substrate"]) == 2


@dataclass(slots=True)
class _SubstrateCalls:
    refresh_drift: list[bool] = field(default_factory=list[bool])
    refreshed: int = 0
    coverages: list[object] = field(default_factory=list[object])


def _managed_substrate(monkeypatch: pytest.MonkeyPatch) -> _SubstrateCalls:
    """``ensure_substrate`` and ``refresh_tree`` replaced; the campaign records the coverage it was handed."""
    calls = _SubstrateCalls()

    def ensure(*_args: object, refresh_drift: bool = False, **_kwargs: object) -> substrate_cmd.Ensured:
        calls.refresh_drift.append(refresh_drift)
        return substrate_cmd.Ensured(replace(expected_substrate_metadata(), generation="g-1"), "missing_coverage")

    def refresh(**_kwargs: object) -> int:
        calls.refreshed += 1
        return 0

    def campaign_run(_mutants: object, _sources: object, cov: object, **_kwargs: object) -> list[MutantResult]:
        calls.coverages.append(cov)
        return []

    monkeypatch.setattr(mutate, "ensure_substrate", ensure)
    monkeypatch.setattr(mutate, "refresh_tree", refresh)
    monkeypatch.setattr(mutate, "run_campaign", campaign_run)
    return calls


def test_a_direct_campaign_brings_its_substrate_up_to_the_tree_and_reads_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `balamut substrate` step: a missing substrate is built, a divergence refreshed into the store."""
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    calls = _managed_substrate(monkeypatch)

    assert mutate.main(_argv(tmp_path, kind="src", paths=["src/x.py"])) == 0

    assert calls.refresh_drift == [True]
    assert calls.refreshed == 1
    assert len(calls.coverages) == 1
    assert isinstance(calls.coverages[0], StoreCoverage)
    assert "substrate: built generation g-1 (reason: missing_coverage)" in capsys.readouterr().err


def test_a_bare_coverage_file_is_read_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A coverage file without metadata records neither how nor on which tree it was measured: nothing to refresh."""
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    (tmp_path / "nope.coverage").write_bytes(b"placeholder")
    calls = _managed_substrate(monkeypatch)

    def empty_coverage(_path: Path, **_kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    monkeypatch.setattr(mutate, "load_coverage_map", empty_coverage)

    assert mutate.main(_argv(tmp_path, kind="src", paths=["src/x.py"])) == 0
    assert calls.refresh_drift == []
    assert calls.refreshed == 0
    assert isinstance(calls.coverages[0], CoverageMap)


def test_a_bare_coverage_file_with_absolute_paths_is_read_relative_to_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    data = CoverageData(basename=str(tmp_path / "nope.coverage"))
    data.set_context("tests/x_test.py::test_x|run")
    data.add_lines({str(load_profile(tmp_path).root / "src/x.py"): {2}})
    data.write()
    calls = _managed_substrate(monkeypatch)

    assert mutate.main(_argv(tmp_path, kind="src", paths=["src/x.py"])) == 0

    [coverage] = calls.coverages
    assert isinstance(coverage, CoverageMap)
    assert coverage.tests_covering("src/x.py", 2) == frozenset({"tests/x_test.py::test_x"})


@pytest.mark.parametrize(
    ("failing", "expected"),
    [(("tests/x_test.py::test_x",), mutate.EXIT_REFRESH_BASELINE_FAILURE), ((), mutate.EXIT_USAGE)],
    ids=["red-tests", "infrastructure"],
)
def test_a_failed_substrate_build_stops_the_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, ...],
    expected: int,
) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    calls = _managed_substrate(monkeypatch)

    def red(*_args: object, **_kwargs: object) -> substrate_cmd.Ensured:
        raise substrate_cmd.SubstrateBuildFailedError("building the coverage substrate exited with 1", failing)

    monkeypatch.setattr(mutate, "ensure_substrate", red)
    out = tmp_path / "refresh-failures.json"
    argv = [*_argv(tmp_path, kind="src", paths=["src/x.py"]), "--refresh-failures-out", str(out)]

    assert mutate.main(argv) == expected
    err = capsys.readouterr().err
    assert "building the coverage substrate exited with 1" in err
    assert calls.coverages == []
    if failing:
        assert "BASELINE tests/x_test.py::test_x" in err
        assert failures.failed_nodeids_read(out) == failing


def test_typed_kind_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "nope.coverage").touch()

    def empty_coverage(_path: Path, **_kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    monkeypatch.setattr(mutate, "load_coverage_map", empty_coverage)

    assert mutate.main(_argv(tmp_path, kind="typed", paths=[])) == 2


@pytest.mark.parametrize("extra", [["--kind", "future-kind"], ["--mechanism", "bogus"]])
def test_unknown_kind_or_mechanism_is_rejected(tmp_path: Path, extra: list[str]) -> None:
    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), *extra]) == 2


def test_an_unknown_kind_is_named_before_the_cache_opens(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mutate.main(_argv(tmp_path, kind="future-kind", paths=[])) == 2

    known = ",".join(sorted(known_kinds()))
    assert capsys.readouterr().err == f"error: unknown --kind ['future-kind']; known: {known}\n"
    assert not (tmp_path / "c.sqlite").exists()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--mutant-timeout", "nan"),
        ("--mutant-timeout", "0"),
        ("--mutant-timeout", "-1"),
        ("--timeout-retry-multiplier", "inf"),
        ("--timeout-retry-multiplier", "0"),
    ],
)
def test_invalid_timeouts_are_rejected(tmp_path: Path, flag: str, value: str) -> None:
    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), flag, value]) == 2


def test_retry_timeout_overflow_is_rejected(tmp_path: Path) -> None:
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--mutant-timeout", "1e308", "--timeout-retries", "2"]

    assert mutate.main([*argv, "--timeout-retry-multiplier", "10"]) == 2


def test_fast_diff_mode_requires_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutate, "prepare_diff", _changed())

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main", "--diff-mode", "fast"]) == 2


def test_diff_defaults_to_full_and_includes_oracle_test_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Kwargs = {}

    def prepare(*_args: object, **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(mutate, "prepare_diff", prepare)

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main"]) == 0
    assert seen["scope"] is None
    assert seen["include_tests"] is True


@pytest.mark.parametrize(("extra", "expected"), [((), None), (("--fresh-substrate-generation", "g-7"), "g-7")])
def test_diff_forwards_substrate_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...], expected: str | None
) -> None:
    seen: Kwargs = {}

    def prepare(*_args: object, **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(mutate, "prepare_diff", prepare)

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main", *extra]) == 0
    assert seen["fresh_generation"] == expected


def test_refresh_baseline_failure_names_the_failing_tests_and_exits_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = ("tests/x_test.py::test_x", "tests/x_test.py::test_y[param]")

    def failing_refresh(*_args: object, **_kwargs: object) -> list[str]:
        raise RefreshBaselineError(failing, detail="pytest exited with 1")

    monkeypatch.setattr(mutate, "prepare_diff", failing_refresh)
    out = tmp_path / "refresh-failures.json"
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main"]

    assert mutate.main([*argv, "--refresh-failures-out", str(out)]) == mutate.EXIT_REFRESH_BASELINE_FAILURE
    assert failures.failed_nodeids_read(out) == failing
    assert mutate.main(argv) == mutate.EXIT_REFRESH_BASELINE_FAILURE


def test_mixed_diff_routes_source_and_oracle_kinds_to_their_own_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    (tmp_path / "nope.coverage").write_bytes(b"placeholder")
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "prepare_diff", _changed("src/x.py", "tests/x_test.py"))
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected("tests/x_test.py::test_x"))
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))

    assert mutate.main([*_argv(tmp_path, kind="src,oracle", paths=[]), "--diff", "origin/main"]) == 0
    assert any(m.path == "src/x.py" and m.family == "src" for m in call.mutants)
    assert any(m.path == "tests/x_test.py" and m.family == "oracle" for m in call.mutants)
    assert all((m.path == "tests/x_test.py") is (m.family == "oracle") for m in call.mutants)


@pytest.mark.parametrize(("diff", "expected"), [(True, None), (False, {"tests/x_test.py::test_x": 3.0})])
def test_substrate_cost_sidecar_overrides_the_store_only_outside_diff_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diff: bool, expected: dict[str, float] | None
) -> None:
    """In diff mode the sidecar already seeded the cost store at the ingest of its generation; laying it over the
    store again would overwrite durations refreshed since then."""
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    sidecar = tmp_path / "substrate.costs.json"
    pytest_costs.write_costs(sidecar, {"tests/x_test.py::test_x": 3.0})
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "prepare_diff", _changed("tests/x_test.py"))
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected("tests/x_test.py::test_x"))
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))
    mode = ["--diff", "origin/main"] if diff else []
    paths = [] if diff else ["tests/x_test.py"]

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=paths), *mode, "--costs", str(sidecar)]) == 0
    assert call.mutants
    assert call.initial_costs == expected


@pytest.mark.parametrize(
    "payload", [{"nested": {"key": "k1"}}, {"findings": "not-a-list"}, {"findings": [{"key": 1}]}, [{"key": None}], []]
)
def test_invalidation_rejects_malformed_or_keyless_input(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--invalidate-sites-from", str(path)]) == 2


def test_invalidation_deletes_reported_keys_and_continues(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        for key in ("k1", "k2"):
            store.put_verdict(
                key=key,
                file="src/x.py",
                lineno=10,
                operator="lang:compare:Lt->LtE",
                original="a < b",
                mutated="a <= b",
                qualname="f",
                func_hash="fh",
                verdict="survived",
                bucket="high",
                covering=[],
                covering_hash="ch",
            )
    finally:
        store.close()
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([{"key": "k1"}]), encoding="utf-8")

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--invalidate-sites-from", str(raw)]) == 0
    reopened = Store(tmp_path / "c.sqlite")
    try:
        assert reopened.get_verdict("k1") is None
        assert reopened.get_verdict("k2") is not None
    finally:
        reopened.close()


def test_requested_stats_path_reaches_the_campaign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))
    stats_path = tmp_path / "campaign-stats.json"

    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--json", "--stats-json", str(stats_path)]

    assert mutate.main(argv) == 0
    assert call.options is not None
    assert call.options.stats_path == stats_path


def test_empty_paths_emit_complete_zero_stats_and_raw_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stats_path = tmp_path / "campaign-stats.json"

    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--json", "--stats-json", str(stats_path)]

    assert mutate.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == []
    snapshot = stats.read_campaign_stats(stats_path)
    assert snapshot is not None
    assert snapshot.complete is True
    assert (snapshot.enumerated, snapshot.executed, snapshot.terminal) == (0, 0, 0)


def test_empty_paths_without_stats_keep_stdout_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--json"]) == 0
    assert not capsys.readouterr().out


def test_stats_path_with_a_missing_parent_is_rejected(tmp_path: Path) -> None:
    stats_path = tmp_path / "missing" / "campaign-stats.json"

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--stats-json", str(stats_path)]) == 2
    assert not stats_path.exists()


def test_collection_error_during_the_campaign_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    closes = 0
    original_close = Store.close

    def failing_campaign(*_args: object, **_kwargs: object) -> list[MutantResult]:
        raise collection.CollectionError("an oracle node id is missing")

    def tracked_close(store: Store) -> None:
        nonlocal closes
        closes += 1
        original_close(store)

    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected("tests/x_test.py::test_x"))
    monkeypatch.setattr(mutate, "run_campaign", failing_campaign)
    monkeypatch.setattr(Store, "close", tracked_close)

    assert mutate.main(_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"])) == 2
    assert capsys.readouterr().err.endswith("error: an oracle node id is missing\n")
    assert closes == 1


def test_agent_json_separates_cold_no_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "class Model:\n    limit: int = 10\n"
    write_file(tmp_path / "src/schema.py", source)
    coverage = tmp_path / "coverage"
    coverage.write_bytes(b"placeholder")
    cold = next(m for m in enumerate_mutants("src/schema.py", source) if m.operator.startswith("lang:const:num:"))

    def load(_path: Path, **_kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    def record(*_args: object, **kwargs: object) -> list[MutantResult]:
        store = kwargs.get("store")
        assert isinstance(store, Store)
        store.put_verdict(
            key="cold-key",
            file=cold.path,
            lineno=cold.lineno,
            operator=cold.operator,
            original=cold.original,
            mutated=cold.mutated,
            qualname="Model",
            func_hash="cold",
            verdict="no_coverage",
            bucket="review",
            covering=[],
            covering_hash="",
        )
        return [MutantResult(cold, MutantStatus.NO_COVERAGE, 0, "cold-key")]

    monkeypatch.setattr(mutate, "load_coverage_map", load)
    monkeypatch.setattr(mutate, "run_campaign", record)

    argv = ["src/schema.py", "--kind", "src", "--coverage", str(coverage), "--cache", str(tmp_path / "c.sqlite")]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--agent-json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["no_coverage"] == 0
    assert report["cold_no_coverage"] == 1
    assert report["cold_kinds"] == {"schema_default": 1}


def test_the_campaign_environment_carries_the_plugin_layout_and_fingerprint_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        textwrap.dedent(
            """
            from balamut.config import init

            init(source=("app",), import_roots=("app",), tests=("checks",), env_prefixes=("APP_",))
            """
        ),
    )
    write_file(tmp_path / "checks/test_x.py", "def test_x():\n    assert 1 < 2\n")
    seen: dict[str, str] = {}

    def record(*_args: object, **_kwargs: object) -> list[MutantResult]:
        seen.update({name: os.environ.get(name, "") for name in mutate.CAMPAIGN_ENV})
        return []

    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected("checks/test_x.py::test_x"))
    monkeypatch.setattr(mutate, "run_campaign", record)

    assert mutate.main(_argv(tmp_path, kind="oracle", paths=["checks/test_x.py"])) == 0
    assert seen["PYTEST_ADDOPTS"].split() == ["-p", "balamut.runtime.plugin"]
    assert seen["BALAMUT_TEST_ROOTS"] == "checks"
    assert seen["PYTEST_FAST_WATCH_DIRS"] == "app,checks"
    assert seen["PYTEST_FAST_ENV_PREFIXES"] == "APP_"


def test_campaign_environment_defaults_unset_marks_and_appends_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``PYTEST_ADDOPTS``/``PYTEST_FAST_MARK`` read as empty strings: a dropped default reads ``None``
    (and a swapped default carries the variable name itself)."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_FAST_MARK", raising=False)
    profile = load_profile(tmp_path)

    env = mutate.campaign_environment(profile, isolation_for(profile))

    assert env["PYTEST_ADDOPTS"] == "-p balamut.runtime.plugin"
    assert env["PYTEST_FAST_MARK"] == ""


@dataclass(slots=True)
class _EngineRun:
    return_code: int
    call: _CampaignCall
    pinned_scope: object


def _engine_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    edit_during_refresh: bool = False,
    campaign_fn: Callable[..., list[MutantResult]] | None = None,
) -> _EngineRun:
    """``mutate.main`` on a pinned scope of one file; refresh and campaign replaced."""
    edited = write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    scope = pinned(tmp_path, "src/x.py")
    scope_path = tmp_path / "scope.json"
    write_scope(scope, scope_path)
    (tmp_path / "nope.coverage").write_bytes(b"placeholder")

    def refresh(*_args: object, **_kwargs: object) -> list[str]:
        if edit_during_refresh:
            edited.write_text("X = 0\n", encoding="utf-8")
        return ["src/x.py"]

    call = _CampaignCall()
    monkeypatch.setattr(mutate, "prepare_diff", refresh)
    monkeypatch.setattr(mutate, "run_campaign", campaign_fn or _recording_campaign(call))
    argv = [*_argv(tmp_path, kind="src", paths=[]), "--diff", "origin/main", "--diff-scope", str(scope_path)]
    return _EngineRun(return_code=mutate.main(argv), call=call, pinned_scope=scope)


def test_engine_hands_the_campaign_a_watch_over_the_pinned_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _engine_run(tmp_path, monkeypatch)

    assert run.return_code == 0
    assert run.call.scope_watch is not None
    assert run.call.scope_watch.scope == run.pinned_scope


def test_engine_exits_scope_changed_when_the_campaign_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def aborted(*_args: object, **_kwargs: object) -> list[MutantResult]:
        raise CampaignScopeChangedError(("src/x.py",))

    assert _engine_run(tmp_path, monkeypatch, campaign_fn=aborted).return_code == mutate.EXIT_SCOPE_CHANGED


def test_engine_stops_before_the_campaign_when_refresh_outlived_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _engine_run(tmp_path, monkeypatch, edit_during_refresh=True)

    assert run.return_code == mutate.EXIT_SCOPE_CHANGED
    assert run.call.calls == 0, "a campaign over mixed revisions never starts"


@pytest.mark.parametrize("edited", ["src/x.py", "tests/test_x.py"])
def test_without_the_refresh_a_direct_campaign_warns_when_the_substrate_describes_another_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], edited: str
) -> None:
    """An edited test changes the covering tests as much as an edited source: both are named."""
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    write_file(tmp_path / "tests/test_x.py", "def test_x():\n    assert True\n")
    coverage = tmp_path / "substrate.coverage"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    substrate.tree_write(coverage, published.generation, substrate.tree_snapshot(make_layout(tmp_path)))
    write_file(tmp_path / edited, "# edited\n")
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = ["src/x.py", "--kind", "src", "--coverage", str(coverage), "--cache", str(tmp_path / "c.sqlite")]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert (
        f"the substrate was measured on another version of {edited}; run `balamut substrate`" in capsys.readouterr().err
    )


# ── the parser defaults ──────────────────────────────────────────────────────


def test_parser_defaults_and_detailed_json(tmp_path: Path) -> None:
    parser = mutate._parser(load_profile(tmp_path))
    defaults = parser.parse_args([])
    assert defaults.workers == 1
    assert defaults.daemons == 0
    assert defaults.timeout_retry_multiplier == 3.0
    assert defaults.detailed_json == ""

    detailed = parser.parse_args(["--detailed-json", str(tmp_path / "d.json")])
    assert detailed.detailed_json == str(tmp_path / "d.json")


def test_parser_takes_the_address_and_the_kinds_from_the_profile(tmp_path: Path) -> None:
    profile = load_profile(tmp_path)

    defaults = mutate._parser(profile).parse_args([])

    assert defaults.address == default_address(profile)
    assert defaults.kind == ",".join(profile.kinds)


def test_parser_reads_counts_as_integers_and_switches_as_booleans(tmp_path: Path) -> None:
    parser = mutate._parser(load_profile(tmp_path))

    defaults = parser.parse_args([])
    given = parser.parse_args(["--workers", "3", "--limit", "2", "--cross-kill", "--flat"])

    assert (defaults.cross_kill, defaults.flat) == (False, False)
    assert (given.cross_kill, given.flat) == (True, True)
    assert (given.workers, given.limit) == (3, 2)


def test_parser_rejects_an_unknown_progress_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parser = mutate._parser(load_profile(tmp_path))

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["--progress", "bogus"])

    assert raised.value.code == 2
    assert "argument --progress: invalid choice: 'bogus'" in capsys.readouterr().err


# ── main(): cache, state dir and stats handling ──────────────────────────────


def test_main_announces_the_cache_and_creates_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "a" / "b" / "c.sqlite"
    ensured = 0

    def tracked_ensure(profile: object) -> None:
        nonlocal ensured
        ensured += 1

    monkeypatch.setattr(mutate, "ensure_state_dir", tracked_ensure)

    argv = ["--kind", "oracle", "--cache", str(cache), "--coverage", str(tmp_path / "nope.coverage")]
    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock")]) == 0

    assert ensured == 1
    assert cache.parent.is_dir()
    assert f"cache: {cache}" in capsys.readouterr().err


def test_run_announces_the_baseline_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mutate.main(_argv(tmp_path, kind="oracle", paths=[])) == 0
    assert "baseline cached:" in capsys.readouterr().err


def test_empty_paths_announce_nothing_to_mutate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mutate.main(_argv(tmp_path, kind="oracle", paths=[])) == 0
    assert "nothing to mutate" in capsys.readouterr().err


def test_invalidation_announces_the_deleted_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path / "c.sqlite")
    store.put_verdict(
        key="k1",
        file="src/x.py",
        lineno=1,
        operator="lang:compare:Lt->LtE",
        original="a < b",
        mutated="a <= b",
        qualname="f",
        func_hash="h",
        verdict="survived",
        bucket="high",
        covering=[],
        covering_hash="",
    )
    store.close()
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([{"key": "k1"}]), encoding="utf-8")

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--invalidate-sites-from", str(raw)]) == 0
    assert "cache invalidation: deleted 1 of 1 keys" in capsys.readouterr().err


def test_main_unlinks_a_stale_stats_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    stats_path = tmp_path / "campaign-stats.json"
    stats_path.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))

    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--json", "--stats-json", str(stats_path)]

    assert mutate.main(argv) == 0
    assert not stats_path.exists()


# ── _cold_records ────────────────────────────────────────────────────────────


def _record(
    file: str,
    lineno: int,
    *,
    operator: str = "lang:compare:Lt->LtE",
    original: str = "a < b",
    mutated: str = "a <= b",
) -> MutantRecord:
    return {
        "file": file,
        "lineno": lineno,
        "operator": operator,
        "verdict": "no_coverage",
        "bucket": "high",
        "original": original,
        "mutated": mutated,
        "covering": [],
        "semantic": [],
    }


def test_cold_records_keeps_a_reachable_body_record_runtime() -> None:
    sources = {"src/x.py": "def f(a, b):\n    return a < b\n"}
    records = [_record("src/x.py", 2)]

    runtime, cold = mutate._cold_records(records, [], sources)

    assert len(runtime) == 1
    assert cold == {}


def test_cold_records_treats_a_missing_source_as_body() -> None:
    records = [_record("src/missing.py", 2)]

    runtime, cold = mutate._cold_records(records, [], {})

    assert len(runtime) == 1
    assert cold == {}


def test_cold_records_resolves_an_unkeyed_cold_record() -> None:
    sources = {"src/schema.py": "class Model:\n    limit: int = 10\n"}
    records = [_record("src/schema.py", 2, operator="lang:const:num:10->11", original="10", mutated="11")]

    runtime, cold = mutate._cold_records(records, [], sources)

    assert runtime == []
    assert cold == {"schema_default": 1}


def test_cold_records_resolves_a_keyed_record_by_the_span_of_its_mutant() -> None:
    """Two statements share the line: only the byte span of the campaign's mutant tells which one was mutated."""
    source = 'class Model:\n    limit: int = 10; KIND = "a"\n'
    mutant = next(m for m in enumerate_mutants("src/schema.py", source) if m.operator.startswith("lang:const:num:"))
    record = _record("src/schema.py", 2, operator=mutant.operator, original=mutant.original, mutated=mutant.mutated)
    record["key"] = "k1"
    results = [MutantResult(mutant, MutantStatus.NO_COVERAGE, 0, "k1")]

    runtime, cold = mutate._cold_records([record], results, {"src/schema.py": source})

    assert runtime == []
    assert cold == {"schema_default": 1}


# ── _refresh_substrate timing ────────────────────────────────────────────────


def test_refresh_substrate_records_a_positive_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = load_profile(tmp_path)
    isolation = isolation_for(profile)
    store = Store(tmp_path / "c.sqlite")
    context = MeasureContext(profile=profile, isolation=isolation)
    cov_path = tmp_path / "coverage"
    timings = stats.empty_campaign_timings()
    metadata = replace(expected_substrate_metadata(), generation="g-1")

    def ensure_fake(*args: object, **kwargs: object) -> substrate_cmd.Ensured:
        return substrate_cmd.Ensured(metadata, "missing_coverage")

    def refresh_fake(**kwargs: object) -> int:
        return 0

    monkeypatch.setattr(mutate, "ensure_substrate", ensure_fake)
    monkeypatch.setattr(mutate, "refresh_tree", refresh_fake)
    args = argparse.Namespace(refresh_failures_out="")

    assert (
        mutate._refresh_substrate(
            args,
            profile=profile,
            store=store,
            context=context,
            cov_path=cov_path,
            timings=timings,
        )
        is None
    )
    assert 0 < timings["coverage_refresh"] < time.perf_counter()


def test_refresh_substrate_forwards_the_ordered_substrate_and_refresh_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_substrate`` receives (profile, isolation, cov_path) with a measured partial, and ``refresh_tree``
    receives every keyword by name (a dropped or swapped argument would break the refresh)."""
    profile = load_profile(tmp_path)
    isolation = isolation_for(profile)
    store = Store(tmp_path / "c.sqlite")
    context = MeasureContext(profile=profile, isolation=isolation)
    cov_path = tmp_path / "coverage"
    timings = stats.empty_campaign_timings()
    metadata = replace(expected_substrate_metadata(), generation="g-1")
    ensured: _EnsureCapture = {"args": (), "kwargs": {}}
    refreshed: dict[str, object] = {}

    def ensure_fake(*args: object, **kwargs: object) -> substrate_cmd.Ensured:
        ensured["args"] = args
        ensured["kwargs"] = kwargs
        return substrate_cmd.Ensured(metadata, "missing_coverage")

    def refresh_fake(**kwargs: object) -> int:
        refreshed["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mutate, "ensure_substrate", ensure_fake)
    monkeypatch.setattr(mutate, "refresh_tree", refresh_fake)
    args = argparse.Namespace(refresh_failures_out="")

    assert (
        mutate._refresh_substrate(
            args,
            profile=profile,
            store=store,
            context=context,
            cov_path=cov_path,
            timings=timings,
        )
        is None
    )

    assert ensured["args"] == (profile, isolation, cov_path)
    assert ensured["kwargs"]["refresh_drift"] is True
    measure = ensured["kwargs"]["measure"]
    assert isinstance(measure, partial)
    assert measure.keywords["isolation"] is isolation
    assert measure.keywords["launcher"] is context.launcher
    assert refreshed["kwargs"] == {
        "cov_path": cov_path,
        "store": store,
        "context": context,
        "phase_timings": timings,
    }


def test_write_empty_stats_forwards_the_timings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_empty_stats(*, complete: bool, timings: object) -> dict[str, object]:
        captured["complete"] = complete
        captured["timings"] = timings
        return {"complete": complete}

    def fake_write_stats(_path: Path, _snapshot: stats.CampaignStats) -> None:
        return None

    monkeypatch.setattr(mutate, "empty_stats", fake_empty_stats)
    monkeypatch.setattr(mutate, "write_campaign_stats", fake_write_stats)
    timings = stats.empty_campaign_timings()
    timings["coverage_refresh"] = 1.0

    mutate._write_empty_stats(tmp_path / "s.json", timings)

    assert captured["complete"] is True
    assert captured["timings"] is timings


def test_timeout_policy_receives_mutant_timeout_before_the_multiplier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[float, float, int]] = []

    def fake(mutant_timeout: float, retry_multiplier: float, retries: int) -> None:
        seen.append((mutant_timeout, retry_multiplier, retries))

    monkeypatch.setattr(mutate, "timeout_policy_error", fake)
    argv = [
        *_argv(tmp_path, kind="oracle", paths=[]),
        "--mutant-timeout",
        "5",
        "--timeout-retry-multiplier",
        "2",
        "--timeout-retries",
        "3",
    ]

    mutate.main(argv)

    assert seen == [(5.0, 2.0, 3)]


def test_main_defaults_the_cache_to_root_then_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path, Path]] = []

    def fake(root: Path, state_dir: Path) -> Path:
        seen.append((root, state_dir))
        return tmp_path / "default.sqlite"

    monkeypatch.setattr(mutate, "default_cache_path", fake)
    argv = [
        "--kind",
        "oracle",
        "--coverage",
        str(tmp_path / "nope.coverage"),
        "--address",
        str(tmp_path / "unused.sock"),
    ]

    mutate.main(argv)

    profile = load_profile(tmp_path)
    assert seen == [(profile.root, profile.state_dir)]


def test_refresh_tests_forwards_the_baseline_refresh_and_collection_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh loop passes the coverage path and store to ``ingest_baseline`` in order, every keyword to
    ``refresh_tests`` and the project root to ``collect_test_nodeids``."""
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    ingested: _EnsureCapture = {"args": (), "kwargs": {}}
    refreshed: _EnsureCapture = {"args": (), "kwargs": {}}
    collected: dict[str, dict[str, object]] = {}

    def ingest_fake(*args: object, **kwargs: object) -> None:
        ingested["args"] = args
        ingested["kwargs"] = kwargs

    def refresh_tests_fake(*args: object, **kwargs: object) -> int:
        refreshed["args"] = args
        refreshed["kwargs"] = kwargs
        return 3

    def collect_fake(_files: object, **kwargs: object) -> frozenset[str]:
        collected["kwargs"] = kwargs
        return frozenset()

    monkeypatch.setattr(mutate, "ingest_baseline", ingest_fake)
    monkeypatch.setattr(mutate, "refresh_tests", refresh_tests_fake)
    monkeypatch.setattr(mutate, "collect_test_nodeids", collect_fake)
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--refresh-tests", "tests/x_test.py"]

    assert mutate.main(argv) == 0

    profile = load_profile(tmp_path)
    assert len(ingested["args"]) == 2
    assert ingested["args"][0] == tmp_path / "nope.coverage"
    assert ingested["kwargs"] == {"root": profile.root}
    assert refreshed["args"] == (["tests/x_test.py"],)
    assert set(refreshed["kwargs"]) == {"store", "context"}
    assert collected["kwargs"]["root"] == profile.root


def test_emit_passes_the_current_keys_to_records_from_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    captured: dict[str, object] = {}

    def records_fake(store_arg: Store, source_files: object, current: object) -> list[MutantRecord]:
        captured["store"] = store_arg
        captured["source_files"] = source_files
        captured["current"] = current
        return []

    monkeypatch.setattr(mutate, "records_from_store", records_fake)
    sources = {"src/x.py": "def f():\n    return 1\n"}

    mutate._emit(_args(), [_result()], store=store, sources=sources, profile=load_profile(tmp_path))

    assert captured["store"] is store
    assert captured["source_files"] == ["src/x.py"]
    assert captured["current"] == {"k1"}


def test_emit_flat_forwards_the_semantic_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    captured: dict[str, object] = {}

    def flat_fake(results: object, *, semantic: object) -> None:
        captured["semantic"] = semantic

    monkeypatch.setattr(mutate, "flat_report", flat_fake)
    profile = load_profile(tmp_path)

    mutate._emit(_args(flat=True), [], store=store, sources={}, profile=profile)

    assert captured["semantic"] == profile.operators.semantic


def test_emit_agent_forwards_cold_counts_to_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    captured: dict[str, object] = {}

    def build_fake(records: object, *, cold_counts: object) -> dict[str, object]:
        captured["cold_counts"] = cold_counts
        return {"counts": {}}

    def fake_records_from_store(*_a: object) -> list[MutantRecord]:
        return []

    def fake_cold_records(
        _records: list[MutantRecord],
        _results: list[MutantResult],
        _sources: dict[str, str],
    ) -> tuple[list[MutantRecord], dict[str, int]]:
        return [], {"schema_default": 1}

    monkeypatch.setattr(mutate, "build_report", build_fake)
    monkeypatch.setattr(mutate, "records_from_store", fake_records_from_store)
    monkeypatch.setattr(mutate, "_cold_records", fake_cold_records)

    mutate._emit(_args(agent_json=True), [], store=store, sources={}, profile=load_profile(tmp_path))

    assert captured["cold_counts"] == {"schema_default": 1}


# ── _warn_stale_substrate ────────────────────────────────────────────────────


def _stale_substrate(tmp_path: Path, files: list[str]) -> None:
    for i, name in enumerate(files):
        write_file(tmp_path / f"src/{name}", f"X{i} = {i}\n")
    coverage = tmp_path / "substrate.coverage"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    substrate.tree_write(coverage, published.generation, substrate.tree_snapshot(make_layout(tmp_path)))
    for name in files:
        write_file(tmp_path / f"src/{name}", "# edited\n")


def test_warn_stale_substrate_lists_five_and_counts_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = [f"m{i}.py" for i in range(6)]
    _stale_substrate(tmp_path, names)
    coverage = tmp_path / "substrate.coverage"

    mutate._warn_stale_substrate(coverage, make_layout(tmp_path))

    err = capsys.readouterr().err
    assert ", ".join(f"src/{n}" for n in names[:5]) + " (+1 more)" in err
    assert f"src/{names[5]}" not in err


def test_warn_stale_substrate_shows_no_overflow_count_for_exactly_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = [f"m{i}.py" for i in range(5)]
    _stale_substrate(tmp_path, names)
    coverage = tmp_path / "substrate.coverage"

    mutate._warn_stale_substrate(coverage, make_layout(tmp_path))

    err = capsys.readouterr().err
    assert "more)" not in err


# ── _emit ────────────────────────────────────────────────────────────────────


def _result(original: str = "a < b", mutated: str = "a <= b") -> MutantResult:
    mutant = Mutant(
        path="src/x.py",
        lineno=1,
        start_byte=0,
        end_byte=4,
        original=original,
        mutated=mutated,
        operator="lang:compare:Lt->LtE",
    )
    return MutantResult(mutant, MutantStatus.SURVIVED, 0, "k1")


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {"agent_json": False, "json": False, "flat": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_emit_default_reads_records_for_the_agent_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[object] = []

    def records_fake(*args: object, **kwargs: object) -> list[MutantRecord]:
        calls.append(1)
        return []

    monkeypatch.setattr(mutate, "records_from_store", records_fake)

    def render_fake(report: object) -> str:
        return "REPORT"

    monkeypatch.setattr(mutate, "render_agent_report", render_fake)

    assert mutate._emit(_args(), [], store=store, sources={}, profile=load_profile(tmp_path)) == 0

    assert calls == [1]
    assert "REPORT" in capsys.readouterr().out


def test_emit_json_skips_the_agent_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[object] = []

    def records_fake(*args: object, **kwargs: object) -> list[MutantRecord]:
        calls.append(1)
        return []

    monkeypatch.setattr(mutate, "records_from_store", records_fake)

    assert mutate._emit(_args(json=True), [], store=store, sources={}, profile=load_profile(tmp_path)) == 0

    assert calls == []


def test_emit_announces_zero_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path / "c.sqlite")

    assert mutate._emit(_args(json=True), [], store=store, sources={}, profile=load_profile(tmp_path)) == 0

    assert "done=0 killed=0 survived=0 timeout=0 no_coverage=0" in capsys.readouterr().err


def test_emit_json_uses_two_space_indent_and_keeps_unicode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path / "c.sqlite")

    assert (
        mutate._emit(
            _args(json=True), [_result("a ≤ b", "a < b")], store=store, sources={}, profile=load_profile(tmp_path)
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "\n  {" in out
    assert "a ≤ b" in out


def test_emit_agent_json_uses_two_space_indent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path / "c.sqlite")
    store.put_verdict(
        key="k1",
        file="src/x.py",
        lineno=1,
        operator="lang:compare:Lt->LtE",
        original="a ≤ b",
        mutated="a < b",
        qualname="f",
        func_hash="h",
        verdict="survived",
        bucket="high",
        covering=[],
        covering_hash="",
    )

    assert (
        mutate._emit(
            _args(agent_json=True),
            [_result("a ≤ b", "a < b")],
            store=store,
            sources={"src/x.py": "a < b\n"},
            profile=load_profile(tmp_path),
        )
        == 0
    )

    out = capsys.readouterr().out
    assert '\n  "score":' in out
    assert "a ≤ b" in out


def test_emit_flat_renders_the_flat_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[object] = []

    def flat_fake(results: list[MutantResult], **kwargs: object) -> None:
        calls.append(results)

    monkeypatch.setattr(mutate, "flat_report", flat_fake)

    assert mutate._emit(_args(flat=True), [_result()], store=store, sources={}, profile=load_profile(tmp_path)) == 0

    assert len(calls) == 1


def test_emit_default_report_counts_the_cold_sites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = "class Model:\n    limit: int = 10\n"
    cold = next(m for m in enumerate_mutants("src/schema.py", source) if m.operator.startswith("lang:const:num:"))
    store = Store(tmp_path / "c.sqlite")
    store.put_verdict(
        key="cold-key",
        file=cold.path,
        lineno=cold.lineno,
        operator=cold.operator,
        original=cold.original,
        mutated=cold.mutated,
        qualname="Model",
        func_hash="cold",
        verdict="no_coverage",
        bucket="review",
        covering=[],
        covering_hash="",
    )
    results = [MutantResult(cold, MutantStatus.NO_COVERAGE, 0, "cold-key")]

    assert mutate._emit(_args(), results, store=store, sources={cold.path: source}, profile=load_profile(tmp_path)) == 0

    assert "▼ COLD: 1 advisory (schema_default=1)" in capsys.readouterr().out


# ── the enumeration cache and its configuration fingerprint ──────────────────


@dataclass(frozen=True, slots=True)
class _Fact:
    names: frozenset[str]
    flag: bool


def test_canonical_keeps_scalars_and_turns_sequences_into_lists() -> None:
    assert mutate._canonical(5) == 5
    assert mutate._canonical("text") == "text"
    assert mutate._canonical(None) is None
    assert mutate._canonical(("a", ["b", 1])) == ["a", ["b", 1]]


def test_canonical_sorts_sets_and_mappings_by_repr() -> None:
    """``repr`` orders values of any types, so a set or a mapping of mixed types has one serialization."""
    mixed_keys = cast("mutate.Canonical", {1: "one", "a": frozenset({"y", "x"})})

    assert mutate._canonical(frozenset({"b", 1, "a"})) == ["a", "b", 1]
    assert mutate._canonical({"b": (2,), "a": frozenset({2, 1})}) == [("a", [1, 2]), ("b", [2])]
    assert mutate._canonical(mixed_keys) == [("a", ["x", "y"]), (1, "one")]


def test_canonical_names_a_dataclass_and_recurses_through_its_fields() -> None:
    fact = cast("mutate.Canonical", _Fact(names=frozenset({"b", "a"}), flag=True))

    assert mutate._canonical(fact) == ["_Fact", [("flag", True), ("names", ["a", "b"])]]


def test_enum_cache_is_one_directory_per_kind_configuration(tmp_path: Path) -> None:
    profile = load_profile(tmp_path)
    overrides = load_kind_overrides(tmp_path)
    one_kind = resolve_kinds(["src"], overrides)
    two_kinds = resolve_kinds(["src", "fault"], overrides)
    directory = profile.state_dir / "tmp" / "enum"

    mutate._enum_cache(profile, one_kind).store("src/x.py", "hash", "src", [])
    mutate._enum_cache(profile, one_kind).store("src/y.py", "hash", "src", [])

    [configured] = directory.iterdir()
    assert re.fullmatch(r"[0-9a-f]{16}", configured.name)
    assert len(list(configured.iterdir())) == 2

    mutate._enum_cache(profile, two_kinds).store("src/x.py", "hash", "src", [])

    assert len(list(directory.iterdir())) == 2


# ── the run tail: messages, daemons, mechanisms, timings, cache ──────────────


def _run_with_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    write_file(tmp_path / "src/x.py", source)
    coverage = tmp_path / "substrate.coverage"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    substrate.tree_write(coverage, published.generation, substrate.tree_snapshot(make_layout(tmp_path)))
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))


def test_run_announces_mutants_and_auto_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "def f(a, b):\n    return a < b\n")
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0

    err = capsys.readouterr().err
    assert "mutants:" in err
    assert "daemons: auto x 1 workers" in err


def test_run_skips_the_mechanisms_line_without_cold_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "def f(a, b):\n    return a < b\n")
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert "mechanisms:" not in capsys.readouterr().err


def test_run_reports_the_mechanisms_line_when_cold_mutants_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "LIMIT = 3\n")
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert "mechanisms: 0 code / 2 cold" in capsys.readouterr().err  # LIMIT = 4 and LIMIT = 2


def test_run_records_a_positive_pytest_collection_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    captured: dict[str, object] = {}

    def campaign(*_args: object, **kwargs: object) -> list[MutantResult]:
        captured["timings"] = kwargs["phase_timings"]
        return []

    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected("tests/x_test.py::test_x"))
    monkeypatch.setattr(mutate, "run_campaign", campaign)

    assert mutate.main(_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"])) == 0

    timings = captured["timings"]
    assert isinstance(timings, dict)
    assert 0 < timings["pytest_collection"] < time.perf_counter()


def test_no_cache_forwards_read_cache_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    captured: dict[str, object] = {}

    def campaign(*_args: object, **kwargs: object) -> list[MutantResult]:
        captured["read_cache"] = kwargs["read_cache"]
        return []

    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", campaign)

    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--no-cache"]
    assert mutate.main(argv) == 0
    assert captured["read_cache"] is False


# ── scope / refresh error reporting ──────────────────────────────────────────


def test_scope_change_during_refresh_names_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _engine_run(tmp_path, monkeypatch, edit_during_refresh=True)

    assert run.return_code == mutate.EXIT_SCOPE_CHANGED
    assert "the scope changed during the refresh" in capsys.readouterr().err


def test_campaign_scope_change_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def aborted(*_args: object, **_kwargs: object) -> list[MutantResult]:
        raise CampaignScopeChangedError(("src/x.py",))

    assert _engine_run(tmp_path, monkeypatch, campaign_fn=aborted).return_code == mutate.EXIT_SCOPE_CHANGED
    assert "error:" in capsys.readouterr().err


def test_refresh_baseline_failure_writes_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing = ("tests/x_test.py::test_x",)

    def failing_refresh(*_args: object, **_kwargs: object) -> list[str]:
        raise RefreshBaselineError(failing, detail="pytest exited with 1")

    monkeypatch.setattr(mutate, "prepare_diff", failing_refresh)
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main"]

    assert mutate.main(argv) == mutate.EXIT_REFRESH_BASELINE_FAILURE
    assert "error:" in capsys.readouterr().err


# ── fast diff filters by the pinned scope ────────────────────────────────────


def _fast_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str) -> list[object]:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    scope = pinned(tmp_path, "src/x.py")
    scope_path = tmp_path / "scope.json"
    write_scope(scope, scope_path)
    (tmp_path / "nope.coverage").write_bytes(b"placeholder")
    seen: list[object] = []

    def capture_filter(mutants: list[Mutant], s: object) -> list[Mutant]:
        seen.append((list(mutants), s))
        return mutants

    monkeypatch.setattr(mutate, "prepare_diff", _changed("src/x.py"))
    monkeypatch.setattr(mutate, "filter_mutants_by_scope", capture_filter)
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [
        *_argv(tmp_path, kind="src", paths=[]),
        "--diff",
        "origin/main",
        "--diff-scope",
        str(scope_path),
        "--diff-mode",
        mode,
    ]
    mutate.main(argv)
    return seen


def test_fast_diff_filters_mutants_by_the_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _fast_diff(tmp_path, monkeypatch, mode="fast")


def test_full_diff_does_not_filter_mutants_by_the_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not _fast_diff(tmp_path, monkeypatch, mode="full")


def test_a_mixed_kind_is_self_covered_by_any_fact(tmp_path: Path) -> None:
    """A kind mixing an oracle (self-covered) and a src (coverage) fact is self-covered: ANY fact suffices."""
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        "from balamut.config import init\n"
        "from balamut.operators.oracle import oracle_kind\n"
        "from balamut.operators.src import src_kind\n"
        'init(kinds=("mixed",), register={"mixed": oracle_kind + src_kind})\n',
    )

    assert mutate.main(["--kind", "mixed"]) == 0


# ── guards and error paths ───────────────────────────────────────────────────


def test_main_rejects_an_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "missing.py"\n')

    assert mutate.main(["--kind", "oracle"]) == mutate.EXIT_USAGE
    assert "invalid [tool.balamut]" in capsys.readouterr().err


def test_run_rejects_a_kind_that_is_not_a_tuple(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        'from balamut.config import init\ninit(kinds=("bad",), register={"bad": 42})\n',
    )

    assert mutate.main(["--kind", "bad"]) == mutate.EXIT_USAGE
    assert "a kind must provide a tuple of operator facts" in capsys.readouterr().err


def test_main_rejects_an_invalid_diff_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scope = tmp_path / "scope.json"
    scope.write_text("not json", encoding="utf-8")

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--diff-scope", str(scope)]) == mutate.EXIT_USAGE
    assert "invalid --diff-scope" in capsys.readouterr().err


def test_refresh_substrate_reports_a_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = load_profile(tmp_path)
    store = Store(tmp_path / "c.sqlite")
    context = MeasureContext(profile=profile, isolation=isolation_for(profile))
    timings = stats.empty_campaign_timings()

    def ensure_fake(*args: object, **kwargs: object) -> substrate_cmd.Ensured:
        return substrate_cmd.Ensured(replace(expected_substrate_metadata()), "built")

    monkeypatch.setattr(mutate, "ensure_substrate", ensure_fake)

    def broken_refresh(**kwargs: object) -> int:
        raise RuntimeError("the refresh broke")

    monkeypatch.setattr(mutate, "refresh_tree", broken_refresh)
    args = argparse.Namespace(refresh_failures_out="")

    assert (
        mutate._refresh_substrate(
            args, profile=profile, store=store, context=context, cov_path=tmp_path / "c", timings=timings
        )
        == mutate.EXIT_USAGE
    )
    assert "the substrate refresh failed" in capsys.readouterr().err


# ── the confirm loop (--refresh-tests) ───────────────────────────────────────


def test_refresh_tests_ingests_the_baseline_and_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")

    def ingest_fake(*args: object, **kwargs: object) -> None:
        return None

    def refresh_tests_fake(*args: object, **kwargs: object) -> int:
        return 3

    monkeypatch.setattr(mutate, "ingest_baseline", ingest_fake)
    monkeypatch.setattr(mutate, "refresh_tests", refresh_tests_fake)
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--refresh-tests", "tests/x_test.py"]

    assert mutate.main(argv) == 0
    assert "refresh tests: 1 file(s), 3 tests re-measured" in capsys.readouterr().err


def test_refresh_tests_ingests_the_baseline_without_store_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    ingest: list[object] = []

    def ingest_fake(*args: object, **kwargs: object) -> None:
        ingest.append(1)

    def refresh_tests_fake(*args: object, **kwargs: object) -> int:
        return 3

    monkeypatch.setattr(mutate, "ingest_baseline", ingest_fake)
    monkeypatch.setattr(mutate, "refresh_tests", refresh_tests_fake)
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--refresh-tests", "tests/x_test.py"]

    assert mutate.main(argv) == 0
    assert ingest == [1]


def test_refresh_tests_records_a_positive_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    captured: dict[str, object] = {}

    def ingest_fake(*args: object, **kwargs: object) -> None:
        return None

    def refresh_tests_fake(*args: object, **kwargs: object) -> int:
        return 3

    monkeypatch.setattr(mutate, "ingest_baseline", ingest_fake)
    monkeypatch.setattr(mutate, "refresh_tests", refresh_tests_fake)
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())

    def campaign(*_args: object, **kwargs: object) -> list[MutantResult]:
        captured["timings"] = kwargs["phase_timings"]
        return []

    monkeypatch.setattr(mutate, "run_campaign", campaign)
    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--refresh-tests", "tests/x_test.py"]

    assert mutate.main(argv) == 0
    timings = captured["timings"]
    assert isinstance(timings, dict)
    assert 0 < timings["coverage_refresh"] < time.perf_counter()


def test_the_refresh_and_the_collection_are_timed_from_their_own_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock is read at the campaign start, then around the refresh and around the collection: neither phase
    is timed from the campaign start."""
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    pass\n")
    readings = iter([100.0, 200.0, 203.0, 300.0, 307.0])
    captured: dict[str, object] = {}

    def ingest_fake(*_args: object, **_kwargs: object) -> None:
        return None

    def refresh_tests_fake(*_args: object, **_kwargs: object) -> int:
        return 3

    def campaign(*_args: object, **kwargs: object) -> list[MutantResult]:
        captured["timings"] = kwargs["phase_timings"]
        return []

    monkeypatch.setattr(mutate, "time", SimpleNamespace(perf_counter=lambda: next(readings)))
    monkeypatch.setattr(mutate, "ingest_baseline", ingest_fake)
    monkeypatch.setattr(mutate, "refresh_tests", refresh_tests_fake)
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", campaign)
    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--refresh-tests", "tests/x_test.py"]

    assert mutate.main(argv) == 0
    timings = captured["timings"]
    assert isinstance(timings, dict)
    assert timings["coverage_refresh"] == 3.0
    assert timings["pytest_collection"] == 7.0


# ── oracle collection over a scope and collection errors ─────────────────────


def test_oracle_collection_over_a_scope_needs_a_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    scope = pinned(tmp_path, "tests/x_test.py")
    scope_path = tmp_path / "scope.json"
    write_scope(scope, scope_path)
    monkeypatch.setattr(mutate, "prepare_diff", _changed("tests/x_test.py"))
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main", "--diff-scope", str(scope_path)]

    assert mutate.main(argv) == mutate.EXIT_USAGE
    assert "an oracle collection over a scope needs a valid coverage substrate" in capsys.readouterr().err


def test_oracle_collection_over_a_scope_builds_the_collection_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a valid substrate and a pinned scope, the oracle collection is keyed by scope, metadata, re-export
    view and root (a dropped keyword would fail the call with a missing-argument error)."""
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    coverage = tmp_path / "nope.coverage"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    substrate.tree_write(coverage, published.generation, substrate.tree_snapshot(make_layout(tmp_path)))
    scope = pinned(tmp_path, "tests/x_test.py")
    scope_path = tmp_path / "scope.json"
    write_scope(scope, scope_path)
    keys: list[object] = []

    def collect(_files: frozenset[str], **kwargs: object) -> frozenset[str]:
        keys.append(kwargs["cache_key"])
        return frozenset()

    monkeypatch.setattr(mutate, "prepare_diff", _changed("tests/x_test.py"))
    monkeypatch.setattr(mutate, "collect_test_nodeids", collect)
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--diff", "origin/main", "--diff-scope", str(scope_path)]

    assert mutate.main(argv) == 0
    expected = collection.build_collection_key(
        frozenset({"tests/x_test.py"}), scope=scope, metadata=published, reexport={}, root=load_profile(tmp_path).root
    )
    assert keys == [expected]


def test_oracle_collection_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")

    def failing_collect(*_args: object, **_kwargs: object) -> frozenset[str]:
        raise collection.CollectionError("an oracle node id is missing")

    monkeypatch.setattr(mutate, "collect_test_nodeids", failing_collect)

    assert mutate.main(_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"])) == mutate.EXIT_USAGE
    assert "the oracle collection failed" in capsys.readouterr().err


def test_emit_reports_a_printing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path / "c.sqlite")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OSError("boom")

    monkeypatch.setattr(mutate, "render_agent_report", boom)

    assert mutate._emit(_args(), [], store=store, sources={}, profile=load_profile(tmp_path)) == 0
    assert "printing the results failed" in capsys.readouterr().err


# ── the --detailed output path ───────────────────────────────────────────────


def test_detailed_defaults_to_the_scratch_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--detailed"]) == 0

    assert call.options is not None
    assert call.options.detailed_path == mutate.campaign_scratch(load_profile(tmp_path)) / "detailed.json"


def test_detailed_creates_the_json_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    detailed = tmp_path / "a" / "b" / "detailed.json"
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))

    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--detailed", "--detailed-json", str(detailed)]

    assert mutate.main(argv) == 0
    assert detailed.parent.is_dir()


def test_detailed_tolerates_an_existing_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")
    detailed = tmp_path / "detailed.json"  # parent tmp_path already exists
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))

    argv = [*_argv(tmp_path, kind="oracle", paths=["tests/x_test.py"]), "--detailed", "--detailed-json", str(detailed)]

    assert mutate.main(argv) == 0


def test_main_creates_the_cache_parent_before_the_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "a" / "b" / "c.sqlite"
    seen: dict[str, bool] = {}
    real_store = Store

    def tracked_store(path: Path) -> Store:
        seen["parent_existed"] = path.parent.is_dir()
        return real_store(path)

    monkeypatch.setattr(mutate, "Store", tracked_store)
    argv = ["--kind", "oracle", "--cache", str(cache), "--coverage", str(tmp_path / "nope.coverage")]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock")]) == 0
    assert seen["parent_existed"] is True


def test_direct_campaign_with_store_coverage_still_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    store = Store(tmp_path / "c.sqlite")
    store.ingest_coverage([("tests/x_test.py::test_x", "src/x.py", 1)])
    store.close()
    calls = _managed_substrate(monkeypatch)

    assert mutate.main(_argv(tmp_path, kind="src", paths=["src/x.py"])) == 0
    assert calls.refresh_drift == [True]


def test_scope_watch_force_rechecks_after_a_recent_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    scope = pinned(tmp_path, "src/x.py")
    scope_path = tmp_path / "scope.json"
    write_scope(scope, scope_path)
    (tmp_path / "nope.coverage").write_bytes(b"placeholder")
    captured: dict[str, ScopeWatch] = {}
    real_watch = mutate.ScopeWatch

    def tracking(scope: DiffScope, *, repo_root: Path) -> ScopeWatch:
        watch = real_watch(scope, repo_root=repo_root)
        captured["watch"] = watch
        return watch

    def prepare(*_args: object, **_kwargs: object) -> list[str]:
        captured["watch"].changed(force=False)  # sets the last-check timestamp
        write_file(tmp_path / "src/x.py", "X = 0\n")
        return ["src/x.py"]

    monkeypatch.setattr(mutate, "ScopeWatch", tracking)
    monkeypatch.setattr(mutate, "prepare_diff", prepare)
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="src", paths=[]), "--diff", "origin/main", "--diff-scope", str(scope_path)]

    assert mutate.main(argv) == mutate.EXIT_SCOPE_CHANGED


def test_refresh_substrate_reports_a_twelve_char_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = load_profile(tmp_path)
    store = Store(tmp_path / "c.sqlite")
    context = MeasureContext(profile=profile, isolation=isolation_for(profile))
    timings = stats.empty_campaign_timings()
    metadata = replace(expected_substrate_metadata(), generation="0123456789abcdef")

    def ensure_fake(*args: object, **kwargs: object) -> substrate_cmd.Ensured:
        return substrate_cmd.Ensured(metadata, "missing_coverage")

    def refresh_fake(**kwargs: object) -> int:
        return 0

    monkeypatch.setattr(mutate, "ensure_substrate", ensure_fake)
    monkeypatch.setattr(mutate, "refresh_tree", refresh_fake)
    args = argparse.Namespace(refresh_failures_out="")

    assert (
        mutate._refresh_substrate(
            args, profile=profile, store=store, context=context, cov_path=tmp_path / "c", timings=timings
        )
        is None
    )
    assert "generation 0123456789ab (reason" in capsys.readouterr().err


def test_run_reports_an_explicit_daemon_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "def f(a, b):\n    return a < b\n")
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--daemons",
        "1",
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert "daemons: 1 x 1 workers" in capsys.readouterr().err


def test_diff_with_store_coverage_needs_no_substrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    store = Store(tmp_path / "c.sqlite")
    store.ingest_coverage([("tests/x_test.py::test_x", "src/x.py", 1)])
    store.close()
    monkeypatch.setattr(mutate, "prepare_diff", _changed("src/x.py"))
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="src", paths=[]), "--diff", "origin/main"]

    assert mutate.main(argv) == 0


def test_diff_with_paths_and_no_coverage_needs_the_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    monkeypatch.setattr(mutate, "prepare_diff", _changed("src/x.py"))
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(_CampaignCall()))
    argv = [*_argv(tmp_path, kind="src", paths=["src/x.py"]), "--diff", "origin/main"]

    assert mutate.main(argv) == mutate.EXIT_USAGE
    assert "no coverage substrate" in capsys.readouterr().err


# ── the --operator prefix filter ─────────────────────────────────────────────


def test_operator_filters_by_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "def f(a, b):\n    return a < b\n")
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--operator",
        "lang:compare",
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert "mutants: 2" in capsys.readouterr().err  # a <= b and a != b


def test_limit_caps_the_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_with_src(tmp_path, monkeypatch, "def f(a, b):\n    return a < b\n")
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(tmp_path / "substrate.coverage"),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--limit",
        "1",
    ]

    assert mutate.main([*argv, "--address", str(tmp_path / "unused.sock"), "--no-refresh-substrate"]) == 0
    assert len(call.mutants) == 1  # of the two the function has
    assert "mutants: 1," in capsys.readouterr().err


@pytest.mark.parametrize(("mechanism", "lines"), [("code", {5}), ("cold", {1})])
def test_mechanism_filters_mutants_to_the_chosen_mechanisms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mechanism: str, lines: set[int]
) -> None:
    """``--mechanism code`` keeps the in-body mutants, ``--mechanism cold`` the module-level ones."""
    write_file(tmp_path / "src/x.py", "LIMIT = 3\n\n\ndef f(a, b):\n    return a < b\n")
    coverage = tmp_path / "substrate.coverage"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    substrate.tree_write(coverage, published.generation, substrate.tree_snapshot(make_layout(tmp_path)))
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "collect_test_nodeids", _collected())
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))
    argv = [
        "src/x.py",
        "--kind",
        "src",
        "--coverage",
        str(coverage),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--address",
        str(tmp_path / "unused.sock"),
        "--no-refresh-substrate",
        "--mechanism",
        mechanism,
    ]

    assert mutate.main(argv) == 0
    assert call.calls == 1
    assert {mutant.lineno for mutant in call.mutants} == lines


def test_no_html_report_is_requested_without_the_option() -> None:
    assert mutate._html_request(argparse.Namespace(html="", html_baseline="")) is None


def test_html_baseline_needs_html(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--html-baseline", str(tmp_path / "old.html")]

    assert mutate.main(argv) == 2
    assert "--html-baseline needs --html" in capsys.readouterr().err


def test_html_into_a_missing_directory_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "missing" / "report.html"

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--html", str(report)]) == 2
    assert "the directory of --html does not exist" in capsys.readouterr().err


def test_an_invalid_html_baseline_stops_before_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = write_file(tmp_path / "old.html", "<html><body>not a report</body></html>")
    call = _CampaignCall()
    monkeypatch.setattr(mutate, "run_campaign", _recording_campaign(call))
    argv = [*_argv(tmp_path, kind="oracle", paths=[]), "--html", str(tmp_path / "r.html"), "--html-baseline", str(old)]

    assert mutate.main(argv) == 2
    assert "invalid --html-baseline" in capsys.readouterr().err
    assert call.calls == 0


def test_an_empty_campaign_still_writes_its_html_report(tmp_path: Path) -> None:
    report = tmp_path / "report.html"

    assert mutate.main([*_argv(tmp_path, kind="oracle", paths=[]), "--html", str(report)]) == 0
    assert read_baseline(report)["counts"] == {"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0}


def _html_campaign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *verdicts: MutantStatus) -> list[str]:
    """argv of a campaign over one module whose single mutant gets ``verdicts`` run after run, with ``--html``."""
    source = "def is_adult(age: int) -> bool:\n    return age >= 18\n"
    write_file(tmp_path / "src/calc.py", source)
    coverage = tmp_path / "coverage"
    coverage.write_bytes(b"placeholder")
    compare = next(m for m in enumerate_mutants("src/calc.py", source) if m.original == "age >= 18")
    pending = iter(verdicts)

    def load(_path: Path, **_kwargs: object) -> CoverageMap:
        return CoverageMap(by_file={})

    def judge(*_args: object, **_kwargs: object) -> list[MutantResult]:
        return [MutantResult(compare, next(pending), 1, "k", ("tests/test_calc.py::test_adult",))]

    monkeypatch.setattr(mutate, "load_coverage_map", load)
    monkeypatch.setattr(mutate, "run_campaign", judge)
    return [
        "src/calc.py",
        "--kind",
        "src",
        "--coverage",
        str(coverage),
        "--cache",
        str(tmp_path / "c.sqlite"),
        "--address",
        str(tmp_path / "unused.sock"),
        "--html",
        str(tmp_path / "report.html"),
    ]


def test_the_html_report_compares_with_the_report_it_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _html_campaign(tmp_path, monkeypatch, MutantStatus.SURVIVED, MutantStatus.KILLED)
    report = tmp_path / "report.html"

    assert mutate.main(argv) == 0
    assert read_baseline(report)["score"] == 0.0
    assert mutate.main([*argv, "--html-baseline", str(report)]) == 0  # the baseline is read before it is replaced

    summary = read_baseline(report)
    assert summary["score"] == 1.0
    assert summary["scope"] == ["src/calc.py"]
    assert [point["score"] for point in summary["history"]] == [0.0]
    assert f"html report: {report}" in capsys.readouterr().err


def _git(root: Path, *args: str) -> str:
    command = ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


def test_the_html_report_names_the_branch_the_commit_and_the_run_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _html_campaign(tmp_path, monkeypatch, MutantStatus.KILLED, MutantStatus.KILLED)
    report = tmp_path / "report.html"
    _git(tmp_path, "init", "-q", "-b", "trunk")
    _git(tmp_path, "add", "src")
    _git(tmp_path, "commit", "-q", "-m", "calc")

    assert mutate.main(argv) == 0
    summary = read_baseline(report)
    assert summary["commit"] == _git(tmp_path, "rev-parse", "HEAD")[:12]
    assert summary["branch"] == "trunk"
    assert re.search(r"<dt>Run time</dt><dd>(under 1 s|\d+ s)</dd>", report.read_text(encoding="utf-8"))

    _git(tmp_path, "checkout", "-q", "--detach")
    assert mutate.main(argv) == 0
    assert read_baseline(report)["branch"] == ""  # a detached HEAD names no branch


def test_a_failed_html_write_is_reported_and_keeps_the_campaign_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _html_campaign(tmp_path, monkeypatch, MutantStatus.KILLED)
    (tmp_path / "report.html").mkdir()

    assert mutate.main(argv) == 0
    err = capsys.readouterr().err
    assert "writing the HTML report failed" in err
    assert "html report:" not in err  # a report that was not written is not announced
    assert not (tmp_path / ".report.html.tmp").exists()


def test_the_html_report_groups_mutants_by_the_risk_areas_of_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        'from balamut.config import init\n\ninit(semantic=(("majority", ("age >=",)),))\n',
    )
    argv = _html_campaign(tmp_path, monkeypatch, MutantStatus.SURVIVED)

    assert mutate.main(argv) == 0

    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "By risk area" in page
    assert "majority" in page


def test_the_report_scope_names_the_pinned_diff_the_base_or_the_paths() -> None:
    pinned_scope = replace(scope(), diff_base_sha="a" * 40, head_sha="b" * 40)
    args = argparse.Namespace(paths=["src"], diff="origin/main")

    assert mutate._scope_label(args, pinned_scope) == (f"diff {'a' * 12}..{'b' * 12}",)
    assert mutate._scope_label(args, None) == ("diff against origin/main",)
    assert mutate._scope_label(argparse.Namespace(paths=["src", "lib"], diff=""), None) == ("src", "lib")
