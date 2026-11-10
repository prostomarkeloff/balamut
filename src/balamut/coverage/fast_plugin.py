"""Forkserver coverage plugin: per-test contexts and a fail-closed merge of worker shards.

Each pytest-fast worker starts its own C tracer after the fork, switches the dynamic context to the node id of
every test (setup, call and teardown included) and saves its coverage and cost shards in the shutdown hook.

The C tracer (``COVERAGE_CORE=ctrace``) is mandatory: the ``sys.monitoring`` core disables a line event after it
first fires, so with dynamic contexts a line would be recorded only under the first test that ran it. The map
from line to tests would be incomplete, real killers would never be selected, and mutants would falsely survive.

The same holds for the body of a measured function under ``@lru_cache``: it runs only on a cache miss, so a value
cached by one test hides the body from every later test of the worker. Each worker therefore clears the caches of
the measured source before every test, as the mutation run clears the target's cache when it applies a mutant.
"""

from __future__ import annotations

import inspect
import math
import multiprocessing
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import TypedDict

import pytest
from _pytest.config import Config
from _pytest.nodes import Item
from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException

from balamut.coverage.costs import CostMap, costs_valid, read_costs, write_costs
from balamut.coverage.failures import FAILURE_FILE_ENV, WORKER_KEY, worker_key
from balamut.runtime.worker_reset import namespace_cache_wrappers, plain_modules

COVERAGE_FILE_ENV = "BALAMUT_COVERAGE_FILE"
COVERAGE_CONFIG_ENV = "BALAMUT_COVERAGE_CONFIG"
COST_FILE_ENV = "BALAMUT_TEST_COST_FILE"
SLOT_ENV = "BALAMUT_COVERAGE_SLOT"

type ShardMap = dict[str, Path]


class WorkerRunResult(TypedDict, total=False):
    nodeid: str
    outcome: str
    duration: float


@dataclass(slots=True)
class _WorkerCoverageState:
    coverage: Coverage
    suffix: str
    cost_file: Path
    cache_clears: tuple[Callable[[], None], ...]
    costs: CostMap = field(default_factory=dict[str, float])


_WORKER_STATE = pytest.StashKey[_WorkerCoverageState]()


class CoverageArtifactError(RuntimeError):
    """The set of coverage or cost shards is incomplete or damaged."""


def pytest_fast_configure_worker(config: Config, workerid: str) -> None:
    """Start the tracer after the fork and before the worker's first test, capturing the whole
    configuration into worker state: the environment is scrubbed before the first test body, so
    nothing after this point may read it back."""
    raw_coverage = os.environ.get(COVERAGE_FILE_ENV)
    if not raw_coverage:
        return
    if config.stash.get(_WORKER_STATE, None) is not None:
        raise RuntimeError("the coverage worker is already configured")
    coverage_file = Path(raw_coverage)
    coverage_file.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{worker_key(os.environ.get(SLOT_ENV, '0'), workerid)}.{os.getpid()}"
    instance = Coverage(
        data_file=str(coverage_file),
        data_suffix=suffix,
        config_file=os.environ.get(COVERAGE_CONFIG_ENV, ""),
    )
    instance.start()
    raw_costs = os.environ.get(COST_FILE_ENV)
    if not raw_costs:
        raise RuntimeError(f"{COST_FILE_ENV} is required for forkserver coverage")
    config.stash[_WORKER_STATE] = _WorkerCoverageState(
        coverage=instance,
        suffix=suffix,
        cost_file=Path(raw_costs),
        cache_clears=source_cache_clears(_source_roots(instance)),
    )


def _source_roots(instance: Coverage) -> tuple[Path, ...]:
    """The measured source directories of the coverage configuration, relative to the project root (the cwd)."""
    raw = instance.get_option("run:source")
    if not isinstance(raw, list):
        return ()
    return tuple(Path(root).resolve() for root in raw)


def source_cache_clears(roots: tuple[Path, ...]) -> tuple[Callable[[], None], ...]:
    """The ``cache_clear`` of every ``@lru_cache`` function defined under ``roots``.

    Called once per worker, after the fork: collection has imported the project, and the wrappers are found in the
    namespaces of the measured modules (``namespace_cache_wrappers``). A wrapper a measured module imports from
    elsewhere stays warm: no measured line hides behind it, and clearing a library cache before every test only
    costs time."""
    verdicts: dict[str, bool] = {}

    def measured(filename: object) -> bool:
        if not isinstance(filename, str):
            return False
        if filename not in verdicts:
            path = Path(filename).resolve()
            verdicts[filename] = any(path.is_relative_to(root) for root in roots)
        return verdicts[filename]

    modules = [module for module in plain_modules() if measured(module.__dict__.get("__file__"))]
    clears: list[Callable[[], None]] = []
    for wrapper in namespace_cache_wrappers(modules):
        code = getattr(inspect.unwrap(wrapper.__wrapped__), "__code__", None)
        if isinstance(code, CodeType) and measured(code.co_filename):
            clears.append(wrapper.cache_clear)
    return tuple(clears)


# The environment keys this plugin owns: read at worker startup into module state, then removed
# from os.environ before each test body so processes the test spawns (nested balamut runs, daemons,
# bare pytest) never inherit the measurement state.
_OWNED_ENV = (
    COVERAGE_FILE_ENV,
    COVERAGE_CONFIG_ENV,
    COST_FILE_ENV,
    SLOT_ENV,
    FAILURE_FILE_ENV,
    "PYTEST_FAST_WORKER_IDENTITY",
    "PYTEST_FAST_WORKERS",
    "PYTEST_FAST_MARK",
    "_PYTEST_FAST_COLLECT",
    "COVERAGE_CORE",
    "PYTEST_ADDOPTS",
)


def scrub_worker_env() -> None:
    """Remove the measurement state from the environment (the tracer keeps it in module state)."""
    for key in _OWNED_ENV:
        os.environ.pop(key, None)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: Item) -> None:
    """Assign the node id to the whole setup/call/teardown protocol of the current test, start it with cold caches
    of the measured source, and make the environment clean for whatever the test itself spawns."""
    scrub_worker_env()
    # pytest-fast starts its workers with ``daemon=True``, and multiprocessing refuses to start a child from a
    # daemonic process (a test's ``multiprocessing.Pool``). The flag changes only this process's own view: the
    # controller still terminates the worker as a daemon, and the worker leaves through ``os._exit``.
    multiprocessing.current_process().daemon = False
    state = item.config.stash.get(_WORKER_STATE, None)
    if state is not None:
        state.coverage.switch_context(item.nodeid)
        for cache_clear in state.cache_clears:
            cache_clear()


def pytest_fast_annotate_result(item: Item, result: WorkerRunResult) -> None:
    """Record the full duration of the test into the process-local cost shard."""
    state = item.config.stash.get(_WORKER_STATE, None)
    nodeid = result.get("nodeid")
    duration = result.get("duration")
    if (
        state is None
        or not isinstance(nodeid, str)
        or isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        return
    state.costs[nodeid] = max(state.costs.get(nodeid, 0.0), float(duration))


def pytest_fast_worker_shutdown(config: Config, workerid: str) -> None:
    """Stop the tracer and save the coverage and cost shards before ``os._exit``."""
    state = config.stash.get(_WORKER_STATE, None)
    if state is None:
        return
    if not state.suffix.partition(".")[0].endswith(workerid):
        raise RuntimeError("the coverage worker suffix does not match the worker id")
    state.coverage.stop()
    state.coverage.save()
    write_costs(state.cost_file.with_name(f"{state.cost_file.name}.{state.suffix}.json"), state.costs)
    del config.stash[_WORKER_STATE]


def _shard_pattern(base: Path, *, cost: bool) -> re.Pattern[str]:
    ending = r"\.json" if cost else ""
    return re.compile(rf"{re.escape(base.name)}\.({WORKER_KEY.pattern})\.[0-9]+{ending}")


def _matching_shards(base: Path, *, cost: bool) -> tuple[Path, ...]:
    pattern = _shard_pattern(base, cost=cost)
    return tuple(path for path in sorted(base.parent.glob(f"{base.name}.s*")) if pattern.fullmatch(path.name))


def _discover_shards(base: Path, *, cost: bool) -> ShardMap:
    pattern = _shard_pattern(base, cost=cost)
    shards: ShardMap = {}
    for path in _matching_shards(base, cost=cost):
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        key = match.group(1)
        if key in shards:
            raise CoverageArtifactError(f"duplicate shard for {key}")
        shards[key] = path
    return shards


def remove_worker_artifacts(coverage_file: Path, cost_file: Path) -> None:
    """Remove only the exact worker shards of a previous unfinished run."""
    for path in (*_matching_shards(coverage_file, cost=False), *_matching_shards(cost_file, cost=True)):
        path.unlink()


def _require_workers(shards: ShardMap, expected: frozenset[str], *, artifact: str) -> None:
    actual = frozenset(shards)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "-"
        extra = ",".join(sorted(actual - expected)) or "-"
        raise CoverageArtifactError(
            f"{artifact}: expected {len(expected)} worker shards; missing={missing}; extra={extra}"
        )


def combine_worker_artifacts(coverage_file: Path, cost_file: Path, *, expected: frozenset[str]) -> None:
    """Merge exactly one coverage and one cost shard per expected worker."""
    if not expected:
        raise ValueError("at least one worker is expected")
    coverage_file.unlink(missing_ok=True)
    cost_file.unlink(missing_ok=True)
    coverage_shards = _discover_shards(coverage_file, cost=False)
    cost_shards = _discover_shards(cost_file, cost=True)
    _require_workers(coverage_shards, expected, artifact="coverage")
    _require_workers(cost_shards, expected, artifact="costs")
    combined = CoverageData(basename=str(coverage_file))
    combined_costs: CostMap = {}
    try:
        for key in sorted(coverage_shards):
            shard = CoverageData(basename=str(coverage_shards[key]))
            shard.read()
            combined.update(shard)
        for key in sorted(cost_shards):
            shard_path = cost_shards[key]
            if not costs_valid(shard_path):
                raise CoverageArtifactError(f"invalid cost shard {shard_path.name}")
            for nodeid, seconds in read_costs(shard_path).items():
                combined_costs[nodeid] = max(combined_costs.get(nodeid, 0.0), seconds)
        combined.write()
        write_costs(cost_file, combined_costs)
    except (CoverageException, OSError, ValueError) as exc:
        coverage_file.unlink(missing_ok=True)
        cost_file.unlink(missing_ok=True)
        raise CoverageArtifactError(f"could not merge worker artifacts: {exc}") from exc
    for path in (*coverage_shards.values(), *cost_shards.values()):
        path.unlink()
