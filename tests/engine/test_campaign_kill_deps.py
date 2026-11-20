"""A kill keeps the functions its killing run actually executed, and the cache holds the kill by them.

The file rule resets a cached verdict on any edit of the mutant's file, of every source file the covering tests
execute and of every covering test file. A kill with recorded dependencies survives edits of functions its run
never entered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from balamut.core.mutants import Mutant
from balamut.coverage.store import Store
from balamut.engine import partition
from balamut.engine.daemon import RunResult
from balamut.engine.results import MutantResult, MutantStatus
from balamut.operators.src import enumerate_mutants
from balamut.runtime.kill_deps import (
    KILL_DEPS_SUFFIX,
    DependencyHasher,
    KillDeps,
    KillValidity,
    kill_deps_decode,
    kill_deps_encode,
)
from bm_support import make_layout, write_file
from bm_support.campaign import (
    FakeIsolation,
    FixedCoverage,
    fixed_support_hash,
    mutant,
    options,
    run,
    stub_campaign_runtime,
    work,
)

_KILLER = "tests/x_test.py::test_x"
_KILLED = [RunResult(1, {}, [_KILLER]), RunResult(0, {}, [])]


def _campaign(tmp_path: Path, mutants: list[Mutant], store: Store, *, workers: int = 1) -> list[MutantResult]:
    return run(tmp_path, mutants, store=store, opts=options(tmp_path, workers=workers, isolation=FakeIsolation()))


def test_confirmed_kill_records_its_lease_and_the_mutated_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compiled mutant carries the module name in co_filename, so the recorder never sees it: the mutant's own
    file (its constants and imports live in the skeleton) is always a dependency."""
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    deps: KillDeps = (("src/x.py", "f"), ("tests/x_test.py", "test_x"))
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(
        monkeypatch, tmp_path, [work(0, item)], _KILLED, runtime_deps=[(("tests/x_test.py", "test_x"),), None]
    )
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = _campaign(tmp_path, [item], store)
        cached = store.get_verdict("key-0")
    finally:
        store.close()

    layout = make_layout(tmp_path)
    assert results[0].status is MutantStatus.KILLED
    assert cached is not None
    assert kill_deps_decode(cached["kill_deps"]) == deps
    support = partition.support_corpus_hash(layout, {})
    assert cached["kill_validity"] == DependencyHasher(layout).validity(deps, support_hash=support)


def test_paired_original_run_adds_its_functions_to_the_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill is proven by two runs; functions only the original passes through are dependencies too: their edit
    could turn the killer red on the original."""
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    write_file(tmp_path / "src/helper.py", "def only_on_the_original():\n    return 2\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item)],
        _KILLED,
        runtime_deps=[(("tests/x_test.py", "test_x"),), (("src/helper.py", "only_on_the_original"),)],
    )
    store = Store(tmp_path / "campaign.sqlite")
    try:
        _campaign(tmp_path, [item], store)
        cached = store.get_verdict("key-0")
    finally:
        store.close()

    assert cached is not None
    assert kill_deps_decode(cached["kill_deps"]) == (
        ("src/helper.py", "only_on_the_original"),
        ("src/x.py", "f"),
        ("tests/x_test.py", "test_x"),
    )
    [(_mutant_ids, _mutant_spec), (_original_ids, original_spec)] = stub.requests
    assert json.loads(original_spec)["original_deps_path"] == f"{tmp_path / 'mutant-1.json'}{KILL_DEPS_SUFFIX}"


@pytest.mark.parametrize(
    ("responses", "recorded", "workers", "status"),
    [
        pytest.param([RunResult(0, {}, [])], [(("src/x.py", "f"),)], 1, MutantStatus.SURVIVED, id="survivor"),
        pytest.param(_KILLED, [None, None], 1, MutantStatus.KILLED, id="kill-without-record"),
        pytest.param(_KILLED, [(("src/x.py", "f"),), None], 2, MutantStatus.KILLED, id="kill-split-across-workers"),
    ],
)
def test_survivor_or_unproven_kill_record_keeps_no_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[RunResult],
    recorded: list[KillDeps | None],
    workers: int,
    status: MutantStatus,
) -> None:
    """Survival is proven by every covering test, not by one run; a kill without a record keeps the file rule, and
    so does a kill of a multi-worker daemon: the request is split across processes, each writing its own file."""
    item = mutant("src/x.py", 10)
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], responses, runtime_deps=recorded)
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = _campaign(tmp_path, [item], store, workers=workers)
        cached = store.get_verdict("key-0")
    finally:
        store.close()

    assert results[0].status is status
    assert cached is not None
    assert (cached["kill_deps"], cached["kill_validity"]) == ("", "")


def test_dependency_file_of_the_previous_mutant_is_not_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    write_file(tmp_path / "src/y.py", "def g():\n    return 2\n")
    first, second = mutant("src/x.py", 10), mutant("src/x.py", 20)
    stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, first), work(1, second)],
        [*_KILLED, *_KILLED],
        runtime_deps=[(("src/y.py", "g"),), None, None, None],
    )
    store = Store(tmp_path / "campaign.sqlite")
    try:
        _campaign(tmp_path, [first, second], store)
        cached = [store.get_verdict("key-0"), store.get_verdict("key-1")]
    finally:
        store.close()

    assert [(verdict["kill_deps"], verdict["kill_validity"] != "") for verdict in cached if verdict is not None] == [
        (kill_deps_encode((("src/x.py", "f"), ("src/y.py", "g"))), True),
        ("", False),
    ]


_SOURCE = (
    "def valid(timeout: int) -> bool:\n    return timeout <= 0\n\n\n"
    "def helper(value: int) -> int:\n    return value + 1\n"
)
_ENTERED: KillDeps = (("src/x.py", "valid"), ("src/y.py", "other"), ("tests/x_test.py", "test_valid"))


def _kill_cached_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deps: KillDeps | None) -> tuple[Store, Mutant]:
    """A mutant in ``valid`` killed by ``test_valid``; its kill dependencies recorded (or not) as a daemon would."""
    write_file(tmp_path / "src/x.py", _SOURCE)
    write_file(tmp_path / "src/y.py", "def other() -> int:\n    return 2\n")
    write_file(tmp_path / "tests/x_test.py", "def test_valid():\n    assert True\n")
    monkeypatch.setattr(partition, "support_corpus_hash", fixed_support_hash)
    item = next(
        m
        for m in enumerate_mutants("src/x.py", _SOURCE)
        if m.operator.startswith("lang:const:num:") and m.original == "0"
    )
    store = Store(tmp_path / "cache.sqlite")
    layout = make_layout(tmp_path)
    items, _prefilled, _hits = partition.partition(
        [item],
        {item.path: _SOURCE},
        FixedCoverage({"tests/x_test.py::test_valid"}),
        layout=layout,
        store=store,
        read_cache=False,
        memo={},
        reexport={},
    )
    kill = partition.KillRecord(deps, DependencyHasher(layout).validity(deps, support_hash="support")) if deps else None
    result = MutantResult(
        item,
        MutantStatus.KILLED,
        1,
        items[0].key,
        items[0].covering,
        ("tests/x_test.py::test_valid",),
    )
    partition.persist_verdict(store, items[0], result, ["tests/x_test.py::test_valid"], kill=kill)
    return store, item


def _rerun_count(tmp_path: Path, store: Store, item: Mutant) -> int:
    source = (tmp_path / item.path).read_text(encoding="utf-8")
    items, _prefilled, _hits = partition.partition(
        [item],
        {item.path: source},
        FixedCoverage({"tests/x_test.py::test_valid"}),
        layout=make_layout(tmp_path),
        store=store,
        read_cache=True,
        memo={},
        reexport={},
    )
    return len(items)


def test_kill_stays_cached_when_an_unentered_function_of_its_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, item = _kill_cached_with(tmp_path, monkeypatch, _ENTERED)
    try:
        write_file(tmp_path / "src/x.py", _SOURCE.replace("return value + 1", "return value + 2"))

        assert _rerun_count(tmp_path, store, item) == 0
    finally:
        store.close()


def test_kill_is_rerun_when_an_entered_function_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, item = _kill_cached_with(tmp_path, monkeypatch, _ENTERED)
    try:
        write_file(tmp_path / "src/y.py", "def other() -> int:\n    return 3\n")

        assert _rerun_count(tmp_path, store, item) == 1
    finally:
        store.close()


def test_kill_of_the_same_function_in_another_module_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy of the function in another module is another mutant: the recorded dependencies describe a run of the
    original function and prove nothing about the copy."""
    store, _item = _kill_cached_with(tmp_path, monkeypatch, _ENTERED)
    copied = _SOURCE + "\n\ndef extra() -> int:\n    return 0\n"
    write_file(tmp_path / "src/copy.py", copied)
    copy = next(
        m
        for m in enumerate_mutants("src/copy.py", copied)
        if m.operator.startswith("lang:const:num:") and m.original == "0" and m.lineno == 2
    )
    try:
        assert _rerun_count(tmp_path, store, copy) == 1
    finally:
        store.close()


def test_kill_without_recorded_dependencies_keeps_the_file_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, item = _kill_cached_with(tmp_path, monkeypatch, None)
    try:
        write_file(tmp_path / "src/x.py", _SOURCE.replace("return value + 1", "return value + 2"))

        assert _rerun_count(tmp_path, store, item) == 1
    finally:
        store.close()


def _cached_kill(*, kill_validity: str, deps: KillDeps) -> partition.CachedVerdict:
    return {
        "verdict": "killed",
        "bucket": "review",
        "covering_hash": "cov",
        "covering_json": "[]",
        "killed_by": [],
        "kill_deps": kill_deps_encode(deps),
        "kill_validity": kill_validity,
    }


def test_kill_without_recorded_validity_keeps_the_file_rule(tmp_path: Path) -> None:
    # A killed verdict whose kill_validity is empty cannot be held by dependencies: the deps branch must not run
    # (it would compare a non-empty current hash against the empty recorded one and drop the kill).
    write_file(tmp_path / "src/x.py", "def valid():\n    return 1\n")
    layout = make_layout(tmp_path)
    cached = _cached_kill(kill_validity="", deps=(("src/x.py", "valid"),))
    validity = KillValidity(layout, support_hash="support")
    assert partition.cached_verdict_holds(cached, "cov", validity, site=("src/x.py", "valid")) is True


def test_kill_deps_not_naming_the_site_keep_the_file_rule(tmp_path: Path) -> None:
    # The dependencies must name the mutated function itself; a record of other functions falls back to the file rule.
    write_file(tmp_path / "src/x.py", "def valid():\n    return 1\n")
    write_file(tmp_path / "src/helper.py", "def helper():\n    return 1\n")
    layout = make_layout(tmp_path)
    cached = _cached_kill(kill_validity="wrong", deps=(("src/helper.py", "helper"),))
    validity = KillValidity(layout, support_hash="support")
    assert partition.cached_verdict_holds(cached, "cov", validity, site=("src/x.py", "valid")) is True
