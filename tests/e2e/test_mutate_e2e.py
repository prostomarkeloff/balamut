"""A real campaign: coverage measured by pytest-fast, mutants judged by live daemons (``balamut mutate``)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from balamut.config import load_profile
from balamut.coverage.measure import MeasureRequest, measure_coverage
from balamut.engine import stats
from balamut.isolation import NoIsolation
from bm_support import make_project

pytestmark = pytest.mark.daemon

type Row = dict[str, str | int | list[str]]


def _measure(root: Path) -> Path:
    profile = load_profile(root)
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = measure_coverage(
            MeasureRequest(profile=profile, coverage_file=root / ".balamut/cov/coverage", workers=2),
            isolation=NoIsolation(),
        )
    finally:
        os.chdir(previous)
    assert result.returncode == 0, result.detail
    return result.coverage_file


def _mutate(root: Path, coverage: Path | None, *extra: str) -> subprocess.CompletedProcess[str]:
    """``balamut mutate`` over the sample module; without ``coverage`` it uses (and maintains) the default substrate."""
    argv = [sys.executable, "-m", "balamut", "mutate", "src/pkg/calc.py", "--kind", "src"]
    if coverage is not None:
        argv += ["--coverage", str(coverage)]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "BALAMUT_"))}
    return subprocess.run(
        [*argv, "--json", "--progress", "none", *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _rows(result: subprocess.CompletedProcess[str]) -> list[Row]:
    assert result.returncode == 0, result.stderr
    rows: list[Row] = json.loads(result.stdout)
    return rows


def _one(rows: list[Row], *, line: int, original: str, mutated: str) -> Row:
    matches = [
        row for row in rows if row["lineno"] == line and row["original"] == original and row["mutated"] == mutated
    ]
    assert len(matches) == 1, [(row["lineno"], row["original"], row["mutated"]) for row in rows]
    return matches[0]


def test_a_real_campaign_runs_a_project_that_errors_on_warnings(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj", pytest_ini='filterwarnings = ["error"]')
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    rows = _rows(_mutate(root, None))

    assert _one(rows, line=3, original="return low", mutated="return None")["status"] == "killed"
    assert "unsupported" not in {str(row["status"]) for row in rows}


def test_a_real_campaign_runs_a_killer_that_starts_a_process_pool(tmp_path: Path) -> None:
    # pytest-fast workers are daemonic processes: the pool must start both in the coverage run and in the paired
    # original of the mutant run, or the killer fails there and the kill is never confirmed.
    root = make_project(
        tmp_path / "proj",
        {
            "tests/test_calc.py": (
                "from multiprocessing import Pool\n\nfrom pkg.calc import clamp\n\n\n"
                "def test_clamp_low_beside_a_pool() -> None:\n"
                "    with Pool(2) as pool:\n"
                "        assert pool.map(abs, [-1, -2]) == [1, 2]\n"
                "    assert clamp(-3, 0, 10) == 0\n"
            )
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    rows = _rows(_mutate(root, None))

    assert _one(rows, line=3, original="return low", mutated="return None")["status"] == "killed"


def test_a_real_campaign_resets_a_cached_caller_between_mutants(tmp_path: Path) -> None:
    # One persistent worker runs the mutants of `bonus` one after another. The `total` wrapper was built at
    # collection, before the fork, where `gc` cannot list it: the lease reset must still clear it, or the next lease
    # reads a total computed under the previous mutant and its verdict needs a fresh fork.
    root = make_project(
        tmp_path / "proj",
        {
            "src/pkg/calc.py": (
                "from functools import lru_cache\n\n\n"
                "def bonus():\n    return 1\n\n\n"
                "@lru_cache(maxsize=None)\ndef total():\n    return 10 + bonus()\n"
            ),
            "tests/test_calc.py": "from pkg.calc import total\n\n\ndef test_total():\n    assert total() == 11\n",
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    result = _mutate(root, None, "--daemons", "1")

    assert {str(row["status"]) for row in _rows(result)} == {"killed"}
    assert "fresh confirmation" not in result.stderr


def test_a_real_campaign_kills_survives_and_reuses_its_cache(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    coverage = _measure(root)
    stats_path = tmp_path / "stats.json"

    first = _rows(_mutate(root, coverage, "--stats-json", str(stats_path)))

    statuses = {str(row["status"]) for row in first}
    assert {"killed", "survived", "no_coverage"} <= statuses
    # `return low` -> `return None` breaks test_clamp_low; no test reaches the `value > high` branch's return.
    killed = _one(first, line=3, original="return low", mutated="return None")
    assert killed["status"] == "killed"
    assert killed["killed_by"] == ["tests/test_calc.py::test_clamp_low"]
    assert _one(first, line=5, original="return high", mutated="return None")["status"] == "no_coverage"
    # `age >= 18` -> `age > 18`: the only test uses 30, so the boundary survives.
    assert _one(first, line=10, original="age >= 18", mutated="age > 18")["status"] == "survived"
    snapshot = stats.read_campaign_stats(stats_path)
    assert snapshot is not None
    assert snapshot.complete
    assert snapshot.enumerated == len(first)

    second = _rows(_mutate(root, coverage, "--stats-json", str(stats_path)))

    assert {row["key"]: row["status"] for row in second} == {row["key"]: row["status"] for row in first}
    again = stats.read_campaign_stats(stats_path)
    assert again is not None
    assert again.executed == 0, "every terminal verdict comes from the cache on an unchanged tree"


def test_a_test_written_for_a_survivor_kills_it_on_the_next_campaign(tmp_path: Path) -> None:
    """No `balamut substrate` step: the first campaign builds the substrate, the new test reaches the coverage through
    a refresh of its file, not a rebuild."""
    root = make_project(tmp_path / "proj")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    def boundary(result: subprocess.CompletedProcess[str]) -> Row:
        return _one(_rows(result), line=10, original="age >= 18", mutated="age > 18")

    first = _mutate(root, None)

    assert boundary(first)["status"] == "survived"
    assert "measuring the suite under coverage" in first.stderr
    tests = root / "tests/test_calc.py"
    killing = "\n\ndef test_adult_from_18() -> None:\n    assert is_adult(18)\n"
    tests.write_text(tests.read_text(encoding="utf-8") + killing, encoding="utf-8")

    second = _mutate(root, None)

    killed = boundary(second)
    assert killed["status"] == "killed"
    assert killed["killed_by"] == ["tests/test_calc.py::test_adult_from_18"]
    assert "measuring the suite under coverage" not in second.stderr
    assert "coverage refresh: 1 files diverged from the substrate" in second.stderr


def test_a_reload_in_one_lease_leaves_the_next_lease_the_imported_definitions(tmp_path: Path) -> None:
    # One persistent worker. Every lease of `double` runs test_reload_calc, which reloads pkg.calc: the module then
    # binds new definitions, while test_calc.py keeps the `triple` it imported at collection. The leases of `triple`
    # come next; test_triple_runs enters whatever the module binds, so each of them is touched either way, and only
    # the imported `triple` can kill. The reload must not outlive its lease.
    root = make_project(
        tmp_path / "proj",
        {
            "src/pkg/calc.py": "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n",
            "tests/test_calc.py": "from pkg.calc import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n",
            "tests/test_reload.py": (
                "import importlib\n\nimport pkg.calc\n\n\n"
                "def test_reload_calc():\n    importlib.reload(pkg.calc)\n    pkg.calc.double(0)\n"
            ),
            "tests/test_module_calls.py": "import pkg.calc\n\n\ndef test_triple_runs():\n    pkg.calc.triple(0)\n",
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    rows = _rows(_mutate(root, None, "--daemons", "1"))

    triple = [row for row in rows if row["lineno"] == 6]
    assert len(triple) == 4  # x / 3, x * 4, x * 2, return None
    assert {str(row["status"]) for row in triple} == {"killed"}
    assert {str(row["status"]) for row in rows if row["lineno"] == 2} == {"survived"}  # the reloads did run


def test_a_constant_argument_folded_into_its_call_is_covered_by_the_call(tmp_path: Path) -> None:
    # CPython folds the constants before ``*args`` into one constant numbered with the call's first line: the lines
    # of those arguments are never recorded, the line of the call is.
    root = make_project(
        tmp_path / "proj",
        {
            "src/pkg/calc.py": (
                'def command(run, *args):\n    return run(\n        "-c",\n        "a=b",\n        "diff",\n'
                "        *args,\n    )\n"
            ),
            "tests/test_calc.py": (
                "from pkg.calc import command\n\n\n"
                "def test_command():\n"
                '    assert command(lambda *parts: list(parts), "x") == ["-c", "a=b", "diff", "x"]\n'
            ),
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    rows = _rows(_mutate(root, None))

    dropped = {(row["lineno"], row["status"]) for row in rows if row["mutated"] == ""}
    assert dropped == {(3, "killed"), (4, "killed"), (5, "killed"), (6, "killed")}


def test_a_closure_built_at_collection_never_lets_its_mutant_survive(tmp_path: Path) -> None:
    # `show` was decorated when the test module was imported, before the mutant: its wrapper keeps the original
    # code, and the test that would kill `isinstance(...)` -> `True` runs the original. A wrapper built inside a
    # test runs the mutant (the touch) and passes. Green covering tests therefore prove nothing about the mutant.
    root = make_project(
        tmp_path / "proj",
        {
            "src/pkg/calc.py": (
                "def describe(fn):\n"
                "    def wrapper(value):\n"
                "        if isinstance(value, int):\n"
                '            return "int " + fn(value)\n'
                '        return "other " + fn(value)\n\n'
                "    return wrapper\n"
            ),
            "tests/test_calc.py": (
                "from pkg.calc import describe\n\n\n"
                "@describe\ndef show(value):\n    return str(value)\n\n\n"
                'def test_text_through_the_imported_definition():\n    assert show("a") == "other a"\n\n\n'
                'def test_int_through_a_fresh_definition():\n    assert describe(str)(1) == "int 1"\n'
            ),
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    rows = _rows(_mutate(root, None))

    assert _one(rows, line=3, original="isinstance(value, int)", mutated="True")["status"] == "unsupported"
    # The fresh wrapper still kills what it reaches.
    assert _one(rows, line=4, original='return "int " + fn(value)', mutated="return None")["status"] == "killed"
