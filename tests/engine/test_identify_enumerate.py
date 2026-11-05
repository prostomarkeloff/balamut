"""Mutant identity (patch target, content key, spec) and campaign enumeration (balamut.engine.identify/enumeration)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from capability import Capability

from balamut.config import KindOverrides, QueueFamily
from balamut.core.mutants import ApplyMechanism, Mutant
from balamut.engine import collection, enumeration, identify
from balamut.operators.concurrency import LockDrop
from balamut.operators.oracle import OracleFact
from balamut.operators.src import enumerate_mutants
from bm_support import make_layout, write_file
from bm_support.scopes import scope


def _mutant(path: str, lineno: int, *, site_end_lineno: int | None = None) -> Mutant:
    return Mutant(
        path=path,
        lineno=lineno,
        start_byte=0,
        end_byte=1,
        original="a",
        mutated="b",
        operator="lang:compare:Lt->LtE",
        site_end_lineno=lineno if site_end_lineno is None else site_end_lineno,
    )


def test_identify_nested_function_targets_enclosing_toplevel() -> None:
    """The patch target of a nested function is its top-level ancestor: the mutated inner rides its co_consts."""
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:const:num:1->2")
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    assert found["spec"]["qualname"] == "outer"
    assert found["spec"]["module"] == "t"
    assert found["qualname"] == "outer"


def test_identify_records_exact_site_columns_for_touch_monitor() -> None:
    src = "def outer(value):\n    return (lambda: 1, value + 1)\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:binop:Add->Sub")
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    line_start = src.encode("utf-8").rfind(b"\n", 0, mutant.start_byte) + 1

    assert found["spec"].get("target_col") == mutant.start_byte - line_start
    assert found["spec"].get("target_end_col") == mutant.end_byte - line_start


def test_identify_records_exact_multiline_site_span_for_touch_monitor() -> None:
    src = "def target(values):\n    return sum(\n        values,\n    )\n"
    mutant = next(
        m for m in enumerate_mutants("t.py", src) if m.operator == "lang:return:None" and m.site_end_lineno > m.lineno
    )

    found = identify.identify(mutant, src, module="t")

    assert found is not None
    source_bytes = src.encode("utf-8")
    start_line = source_bytes.rfind(b"\n", 0, mutant.start_byte) + 1
    end_line = source_bytes.rfind(b"\n", 0, mutant.end_byte) + 1
    assert found["spec"].get("target_col") == mutant.start_byte - start_line
    assert found["spec"].get("target_end_lineno") == mutant.site_end_lineno
    assert found["spec"].get("target_end_col") == mutant.end_byte - end_line


def test_identify_reuses_function_text_within_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    src = "def total() -> int:\n    return 1 + 2\n"
    mutants = [m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:const:num:")]
    original_function_source = identify.function_source
    calls = 0

    def counting(source: str, qualname: str, *, lineno: int | None = None) -> str | None:
        nonlocal calls
        calls += 1
        return original_function_source(source, qualname, lineno=lineno)

    monkeypatch.setattr(identify, "function_source", counting)
    cache: identify.IdentityBaseCache = {}

    found = [identify.identify(m, src, module="t", identity_cache=cache) for m in mutants]

    assert all(item is not None for item in found)
    assert calls == 1


def test_identify_module_level_mutant_is_unsupported() -> None:
    src = "TIMEOUT = 30\ndef f():\n    return 1\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:const:num:30->31")
    assert identify.identify(mutant, src, module="t") is None


def test_identify_default_mutant_sets_defaults_mechanism_and_slot() -> None:
    src = "def f(a, x=1):\n    return a\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.default_index is not None)
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    spec = found["spec"]
    assert spec.get("mechanism") == "defaults"
    assert spec.get("default_index") == 0
    assert spec.get("default_literal") == "2"
    assert spec["qualname"] == "f"


def test_identify_carries_bytecode_fields_for_a_single_line_constant() -> None:
    src = "def f():\n    return 1\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:const:num:1->2")
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    assert found["spec"].get("bc_kind") == "const"
    assert found["spec"].get("bc_mutated") == "2"


def test_identify_injects_yield_points_into_concurrency_mutants_under_schedule_fuzz() -> None:
    src = "import asyncio\n\nasync def f(lock):\n    async with lock:\n        await asyncio.sleep(0)\n"
    start = src.index("lock:")
    mutant = Mutant(
        "t.py",
        4,
        start,
        start + 4,
        "lock",
        "nullcontext()",
        "std:asyncio:lock-drop",
        fact=LockDrop(),
        family="concurrency",
    )
    plain = identify.identify(mutant, src, module="t")
    fuzzed = identify.identify(mutant, src, module="t", schedule_fuzz=True)
    assert plain is not None
    assert fuzzed is not None
    assert "balamut_fuzz_yield" not in plain["spec"]["mutated_source"]
    assert "balamut_fuzz_yield" in fuzzed["spec"]["mutated_source"]


def test_filter_by_mechanism_code_keeps_body_cold_keeps_module_level() -> None:
    src = "TIMEOUT = 30\ndef f(a, b):\n    return a < b\n"
    mutants = enumerate_mutants("t.py", src)
    sources = {"t.py": src}
    code_ops = {m.operator for m in enumeration.filter_by_mechanism(mutants, sources, frozenset({ApplyMechanism.CODE}))}
    assert any(op.startswith("lang:compare") for op in code_ops)
    assert not any(op.startswith("lang:const") for op in code_ops)
    cold_ops = {m.operator for m in enumeration.filter_by_mechanism(mutants, sources, frozenset({ApplyMechanism.COLD}))}
    assert any(op.startswith("lang:const") for op in cold_ops)
    assert not any(op.startswith("lang:compare") for op in cold_ops)


def test_mechanism_breakdown_counts_code_vs_cold() -> None:
    src = "TIMEOUT = 30\ndef f(a, b):\n    return a < b\n"
    breakdown = enumeration.mechanism_breakdown(enumerate_mutants("t.py", src), {"t.py": src})
    assert breakdown["code"] >= 1
    assert breakdown["cold"] >= 1


def test_fast_mode_keeps_only_mutants_intersecting_changed_span() -> None:
    pinned = scope({"src/x.py": (10, 21)})
    mutants = [
        _mutant("src/x.py", 10),
        _mutant("src/x.py", 20, site_end_lineno=21),
        _mutant("src/x.py", 30),
        _mutant("src/y.py", 10),
    ]

    kept = enumeration.filter_mutants_by_scope(mutants, pinned)

    assert [m.lineno for m in kept] == [10, 20]


def test_fast_mode_uses_full_multiline_span() -> None:
    multiline = _mutant("src/x.py", 10, site_end_lineno=12)

    assert enumeration.filter_mutants_by_scope([multiline], scope({"src/x.py": (12,)})) == [multiline]


def _enumerate(
    root: Path,
    paths: list[str],
    kinds: list[str],
    *,
    paths_by_kind: enumeration.PathsByKind | None = None,
    overrides: KindOverrides | None = None,
) -> list[Mutant]:
    mutants, _sources, _reexport = enumeration.enumerate_paths(
        paths,
        kinds,
        layout=make_layout(root),
        resolved=enumeration.resolve_kinds(kinds, overrides or KindOverrides()),
        paths_by_kind=paths_by_kind,
    )
    return mutants


def test_enumerate_oracle_keeps_collectable_test_file(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/test_calc.py", "def test_x():\n    assert 1 < 2\n")

    mutants = _enumerate(tmp_path, ["tests/test_calc.py"], ["oracle"])

    assert any(m.family == "oracle" for m in mutants)


def test_enumerate_oracle_skips_a_non_collectable_helper(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/helpers.py", "def check():\n    assert 1 < 2\n")

    assert _enumerate(tmp_path, ["tests/helpers.py"], ["oracle"]) == []


def test_direct_explicit_test_path_preserves_cross_kind_enumeration(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")

    mutants = _enumerate(tmp_path, ["tests/x_test.py"], ["src", "oracle"])

    assert {m.family for m in mutants} == {"src", "oracle"}


def test_enumerate_walks_directories_and_reports_root_relative_paths(tmp_path: Path) -> None:
    write_file(tmp_path / "src/pkg/a.py", "def f(a, b):\n    return a < b\n")
    write_file(tmp_path / "src/pkg/b.py", "def g(a):\n    return a + 1\n")
    # A source file outside the requested path must not be collected: `_files(root, raw)` joins
    # root/raw, and a swap of the two would walk the whole project root instead.
    write_file(tmp_path / "unrelated.py", "def z():\n    return 1\n")

    mutants, sources, _reexport = enumeration.enumerate_paths(
        ["src"], ["src"], layout=make_layout(tmp_path), resolved=enumeration.resolve_kinds(["src"], KindOverrides())
    )

    assert {m.path for m in mutants} == {"src/pkg/a.py", "src/pkg/b.py"}
    assert set(sources) == {"src/pkg/a.py", "src/pkg/b.py"}


def test_paths_by_kind_routes_each_kind_to_its_own_files(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")

    mutants = _enumerate(
        tmp_path,
        ["src/x.py", "tests/x_test.py"],
        ["src", "oracle"],
        paths_by_kind={"src": frozenset({"src/x.py"}), "oracle": frozenset({"tests/x_test.py"})},
    )

    assert all((m.path == "tests/x_test.py") is (m.family == "oracle") for m in mutants)
    assert {m.path for m in mutants} == {"src/x.py", "tests/x_test.py"}


def test_concurrency_kind_uses_the_configured_queue_families(tmp_path: Path) -> None:
    write_file(
        tmp_path / "src/jobs.py",
        "from sqlalchemy import select\n\ndef claim(token):\n    return select(Job).where(Job.lease_token == token)\n",
    )
    from balamut.operators.concurrency import LockDrop, NowShiftCollapse, OnConflictDrop, QueueCompareDrop, SkipLockDrop

    jobs = QueueFamily(model="Job", family="jobs", fence=("lease_token",))
    concurrency = (LockDrop(), SkipLockDrop(), OnConflictDrop(), NowShiftCollapse(), QueueCompareDrop((jobs,)))
    overrides = KindOverrides(kinds={"concurrency": concurrency})

    plain = _enumerate(tmp_path, ["src/jobs.py"], ["concurrency"])
    configured = _enumerate(tmp_path, ["src/jobs.py"], ["concurrency"], overrides=overrides)

    assert not [m for m in plain if "fence" in m.operator]
    assert [m for m in configured if "fence" in m.operator]


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    with pytest.raises(enumeration.UnknownKindError, match="future-kind"):
        _enumerate(tmp_path, ["src/x.py"], ["future-kind"])


def test_custom_kind_is_registered_through_the_config_registry(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    @dataclass(frozen=True, slots=True)
    class CustomFact:
        def enumerate(self, path: str, source: str) -> list[Mutant]:
            return [Mutant(path, 2, source.index("1"), source.index("1") + 1, "1", "7", "custom:one->seven")]

    overrides = KindOverrides(kinds={"custom": (CustomFact(),)})
    mutants = _enumerate(tmp_path, ["src/x.py"], ["custom"], overrides=overrides)

    assert [m.operator for m in mutants] == ["custom:one->seven"]


def test_custom_kind_rejects_a_fact_without_a_producing_protocol(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    @dataclass(frozen=True, slots=True)
    class UselessFact:
        pass

    overrides = KindOverrides(kinds={"useless": (UselessFact(),)})

    with pytest.raises(enumeration.UnknownKindError, match="Sites or Enumerates"):
        _enumerate(tmp_path, ["src/x.py"], ["useless"], overrides=overrides)


def test_expand_oracle_covering_requires_exact_collected_nodeid() -> None:
    base = "tests/x_test.py::test_foo"
    collected = frozenset({f"{base}[a]", f"{base}[b]", "tests/x_test.py::test_bar", "tests/x_test.py::test_foobar[a]"})

    assert identify.expand_oracle_covering(base, collected) == {f"{base}[a]", f"{base}[b]"}
    assert identify.expand_oracle_covering("tests/x_test.py::test_bar", collected) == {"tests/x_test.py::test_bar"}
    with pytest.raises(collection.CollectionError):
        identify.expand_oracle_covering(base, frozenset())
    with pytest.raises(collection.CollectionError):
        identify.expand_oracle_covering("tests/x_test.py::test_gone", collected)


def test_oracle_collect_files_includes_reexport_wrapper() -> None:
    def mutant(path: str, operator: str, fact: Capability | None = None) -> Mutant:
        return Mutant(
            path=path, lineno=1, start_byte=0, end_byte=1, original="a", mutated="b", operator=operator, fact=fact
        )

    mutants = [
        mutant("tests/x_test.py", "lang:assert:negate", OracleFact()),
        mutant("tests/shared/cases.py", "pytest:raises", OracleFact()),
        mutant("tests/y_test.py", "lang:compare:Lt"),
    ]
    reexport = {
        ("tests/shared/cases.py", "test_foo"): "tests/shared/cases_test.py",
        ("tests/other/z_cases.py", "test_bar"): "tests/other/z_test.py",
    }

    assert identify.oracle_collect_files(mutants, reexport) == frozenset(
        {
            "tests/x_test.py",
            "tests/shared/cases.py",
            "tests/shared/cases_test.py",
        }
    )


def test_oracle_collect_file_is_the_wrapper_of_a_reexported_name(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    reexport = {("tests/shared/cases.py", "TestCase"): "tests/shared/cases_test.py"}

    assert identify.oracle_collect_file("tests/x_test.py", "test_x", reexport, layout=layout) == "tests/x_test.py"
    assert (
        identify.oracle_collect_file("tests/shared/cases.py", "TestCase.test_a", reexport, layout=layout)
        == "tests/shared/cases_test.py"
    )
    assert identify.oracle_collect_file("tests/shared/cases.py", "test_other", reexport, layout=layout) is None
    assert identify.oracle_nodeid("tests/x_test.py", "TestCase.test_a") == "tests/x_test.py::TestCase::test_a"


def test_db_kind_loads_from_the_builtin_registry(tmp_path: Path) -> None:
    write_file(
        tmp_path / "src/x.py",
        "def active(stmt, user_id):\n    return stmt.where(User.id == user_id, User.active)\n",
    )

    mutants = _enumerate(tmp_path, ["src/x.py"], ["db"])

    assert mutants
    assert all(m.family == "db" for m in mutants)


def test_typed_kind_no_longer_exists(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    with pytest.raises(enumeration.UnknownKindError, match="typed"):
        _enumerate(tmp_path, ["src/x.py"], ["typed"])


def test_identify_uses_the_last_newline_before_the_site() -> None:
    """A site after several newlines is column-counted from the line it sits on, not the first line."""
    src = "def f(a):\n    if a:\n        return 1 + 1\n    return a\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:binop:Add->Sub")
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    source_bytes = src.encode("utf-8")
    line_start = source_bytes.rfind(b"\n", 0, mutant.start_byte) + 1
    assert found["spec"].get("target_col") == mutant.start_byte - line_start


def test_identify_accounts_for_a_leading_blank_line() -> None:
    """A newline at byte zero still counts as a line boundary for column math."""
    src = "\ndef f(a, x=1):\n    return a\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.default_index is not None)
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    source_bytes = src.encode("utf-8")
    line_start = source_bytes.rfind(b"\n", 0, mutant.start_byte) + 1
    end_line_start = source_bytes.rfind(b"\n", 0, mutant.end_byte) + 1
    assert found["spec"].get("target_col") == mutant.start_byte - line_start
    assert found["spec"].get("target_end_col") == mutant.end_byte - end_line_start


def test_identify_records_the_function_start_byte_offset() -> None:
    """The function start byte counts every line before the def, not just the first."""
    src = "import os\n\n\ndef outer(value):\n    return (lambda: 1, value + 1)\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:binop:Add->Sub")
    found = identify.identify(mutant, src, module="t")
    assert found is not None
    assert found["func_start_byte"] == src.encode("utf-8").index(b"def outer")


def test_identify_does_not_record_default_slot_for_a_code_mechanism() -> None:
    """A default slot rides only a DEFAULTS-mechanism mutant; a CODE mutant records no default_index."""
    src = "def f(a, b):\n    return a < b\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:compare"))
    code_mutant = replace(mutant, default_index=0, fact=None)
    found = identify.identify(code_mutant, src, module="t")
    assert found is not None
    assert "default_index" not in found["spec"]


def test_load_builtin_kind_splits_on_the_first_colon_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(enumeration._BUILTIN_KINDS, "multi", "pkg.mod:attr:extra")
    calls: list[str] = []

    def fake_import(name: str) -> type:
        calls.append(name)
        return type("Mod", (), {"attr:extra": "value"})

    monkeypatch.setattr(enumeration, "import_module", fake_import)

    assert enumeration.load_builtin_kind("multi") == "value"
    assert calls == ["pkg.mod"]


def test_custom_kind_rejects_a_mixed_fact_tuple(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")

    @dataclass(frozen=True, slots=True)
    class CustomFact:
        def enumerate(self, path: str, source: str) -> list[Mutant]:
            return [Mutant(path, 2, source.index("1"), source.index("1") + 1, "1", "7", "custom:one->seven")]

    @dataclass(frozen=True, slots=True)
    class UselessFact:
        pass

    overrides = KindOverrides(kinds={"mixed": (CustomFact(), UselessFact())})

    with pytest.raises(enumeration.UnknownKindError, match="Sites or Enumerates"):
        _enumerate(tmp_path, ["src/x.py"], ["mixed"], overrides=overrides)


def test_enumerate_builds_reexport_index_when_any_fact_is_self_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 < 2\n")

    @dataclass(frozen=True, slots=True)
    class PlainFact:
        def enumerate(self, path: str, source: str) -> list[Mutant]:
            return []

    calls: list[object] = []

    def fake_reexport(layout: object) -> dict[tuple[str, str], str]:
        calls.append(layout)
        return {}

    monkeypatch.setattr(enumeration, "reexport_index", fake_reexport)
    overrides = KindOverrides(kinds={"mixed": (OracleFact(), PlainFact())})

    enumeration.enumerate_paths(
        ["tests/x_test.py"],
        ["mixed"],
        layout=make_layout(tmp_path),
        resolved=enumeration.resolve_kinds(["mixed"], overrides),
    )

    assert calls


def test_enumerate_skips_self_covered_kind_but_keeps_other_kinds(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")

    mutants = _enumerate(tmp_path, ["src/x.py"], ["oracle", "src"])

    assert {m.family for m in mutants} == {"src"}


def test_enumerate_keeps_self_covered_mutants_of_a_reexported_non_test_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "src/shared.py", "def test_foo():\n    assert 1 < 2\n")

    def fake_reexport(layout: object) -> dict[tuple[str, str], str]:
        _ = layout
        return {("src/shared.py", "test_foo"): "tests/wrapper_test.py"}

    monkeypatch.setattr(enumeration, "reexport_index", fake_reexport)

    mutants = _enumerate(tmp_path, ["src/shared.py"], ["oracle"])

    assert any(m.family == "oracle" for m in mutants)


def test_enumerate_skips_files_without_a_kind_but_keeps_later_files(tmp_path: Path) -> None:
    write_file(tmp_path / "src/a.py", "def f():\n    return 1\n")
    write_file(tmp_path / "src/b.py", "def g(a, b):\n    return a < b\n")

    mutants = _enumerate(tmp_path, ["src"], ["src"], paths_by_kind={"src": frozenset({"src/b.py"})})

    assert {m.path for m in mutants} == {"src/b.py"}


def test_phase_add_accumulates_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self._values = iter([101.0, 203.0])

        def perf_counter(self) -> float:
            return next(self._values)

    monkeypatch.setattr(enumeration, "time", FakeClock())
    timings: dict[str, float] = {}

    enumeration._phase_add(timings, "phase", 100.0)
    assert timings["phase"] == 1.0
    enumeration._phase_add(timings, "phase", 200.0)
    assert timings["phase"] == 4.0


def test_enumerate_records_phase_timings(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f(a, b):\n    return a < b\n")
    timings: dict[str, float] = {}

    enumeration.enumerate_paths(
        ["src/x.py"],
        ["src"],
        layout=make_layout(tmp_path),
        resolved=enumeration.resolve_kinds(["src"], KindOverrides()),
        phase_timings=timings,
    )

    assert "discovery_reexport" in timings
    assert "mutation_enumeration" in timings


def test_identify_disambiguates_same_named_siblings_by_site_line() -> None:
    """Two top-level ``def f`` bodies share one qualname; the site line picks the second, not the first."""
    src = "def f():\n    return 1\n\n\ndef f():\n    return 2\n"
    mutant = next(m for m in enumerate_mutants("t.py", src) if m.operator == "lang:const:num:2->3")

    found = identify.identify(mutant, src, module="t")

    assert found is not None
    assert found["qualname"] == "f"
    assert "return 2" in found["func_src"]
    assert "return 1" not in found["func_src"]
    # The function start byte is the second sibling's ``def`` (the last one), not byte zero of the module.
    assert found["func_start_byte"] == src.encode("utf-8").rindex(b"def f()")


def test_files_resolves_a_single_source_file_under_the_root(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", "def f():\n    return 1\n")
    write_file(tmp_path / "src/other.py", "def g():\n    return 2\n")

    assert enumeration._files(tmp_path, "src/x.py") == [tmp_path / "src/x.py"]


def test_mutant_cache_round_trips_the_enumeration_unchanged(tmp_path: Path) -> None:
    """The content-addressed cache restores the exact mutants (facts included) for the same source and kind."""
    src = "def f(a, b):\n    return a < b\n"
    mutants = enumerate_mutants("src/x.py", src)
    cache = enumeration.MutantCache(tmp_path / "cache")
    source_hash = hashlib.sha256(src.encode()).hexdigest()

    assert cache.load("src/x.py", source_hash, "src") is None
    cache.store("src/x.py", source_hash, "src", mutants)
    assert cache.load("src/x.py", source_hash, "src") == mutants


def test_mutant_cache_misses_on_a_changed_source_or_kind(tmp_path: Path) -> None:
    """A different source hash or kind never returns the stored mutants."""
    src = "def f(a, b):\n    return a < b\n"
    mutants = enumerate_mutants("src/x.py", src)
    cache = enumeration.MutantCache(tmp_path / "cache")
    source_hash = hashlib.sha256(src.encode()).hexdigest()
    cache.store("src/x.py", source_hash, "src", mutants)

    assert cache.load("src/x.py", hashlib.sha256(b"changed").hexdigest(), "src") is None
    assert cache.load("src/x.py", source_hash, "other") is None
    assert cache.load("other/x.py", source_hash, "src") is None


def test_mutant_cache_ignores_a_corrupted_pickle(tmp_path: Path) -> None:
    """A truncated or foreign file falls back to a miss, never a decode error."""
    cache = enumeration.MutantCache(tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    key = hashlib.sha256(b"x\0y\0z").hexdigest()
    (tmp_path / "cache" / f"{key}.pickle").write_bytes(b"not a pickle")

    assert cache.load("x", "y", "z") is None
