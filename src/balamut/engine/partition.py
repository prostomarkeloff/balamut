"""Split a campaign's mutants into cached verdicts and the work to execute; persist verdicts.

A cached verdict holds while its covering hash matches: the hash of the test-support corpus, the mutant's file,
the source files its covering tests execute, the covering test files and the covering node ids themselves. A
kill with recorded dependencies holds instead while the functions its killing run executed are unchanged.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from balamut.config import DEFAULT_SEMANTIC, Layout
from balamut.core.hashing import covering_hash, file_hash, mutant_key, sha
from balamut.core.mutants import Mutant
from balamut.core.semantic import SemanticPatterns, semantic_classes
from balamut.core.triage import Bucket, classify
from balamut.coverage.discovery import ReexportIndex
from balamut.coverage.store import CachedVerdict, CostMap, Store
from balamut.engine.identify import (
    IdentityBaseCache,
    expand_oracle_covering,
    identify,
    oracle_collect_file,
    oracle_nodeid,
)
from balamut.engine.results import MutantResult, MutantStatus
from balamut.operators.facts import SelfCovered
from balamut.runtime.kill_deps import KillDep, KillValidity, kill_deps_decode, kill_deps_encode
from balamut.runtime.plugin import MutantSpec

type FileHashMemo = dict[str, str]
type ResultsByIndex = dict[int, MutantResult]
type SourceCache = dict[str, str]
type CoveredByCache = dict[tuple[str, ...], AbstractSet[str]]

_EMPTY: frozenset[str] = frozenset()


class CoverageLike(Protocol):
    """A coverage source: a coverage map (full mode) or the store (diff mode)."""

    def tests_covering(self, path: str, lineno: int, /) -> AbstractSet[str]: ...

    def files_covered_by(self, test_ids: set[str], /) -> AbstractSet[str]: ...


@dataclass(frozen=True, slots=True)
class Work:
    index: int
    mutant: Mutant
    covering: tuple[str, ...]
    spec: MutantSpec
    key: str  # the content-addressed cache key
    func_hash: str
    qualname: str
    covering_hash: str
    bucket: Bucket = Bucket.REVIEW


def _memo_hash(root: Path, path: str, memo: FileHashMemo) -> str:
    cached = memo.get(path)
    if cached is not None:
        return cached
    value = file_hash(root / path)
    memo[path] = value
    return value


def support_corpus_hash(layout: Layout, memo: FileHashMemo) -> str:
    """The hash of the test-support corpus: Python files of the test trees that are not collectable tests.

    Tests import conftest, support and helper modules, so an edit there changes kills and survivals under the
    same covering tests while being in no covering test file. Constant within a campaign: computed once."""
    support: set[str] = set()
    for test_root in layout.tests:
        base = layout.root / test_root
        if base.is_dir():
            support.update(
                path.relative_to(layout.root).as_posix()
                for path in base.rglob("*.py")
                if not layout.is_test_file(path.name)
            )
    return covering_hash([_memo_hash(layout.root, file, memo) for file in sorted(support)])


def _covering_files(covering: tuple[str, ...]) -> list[str]:
    return sorted({nodeid.partition("::")[0] for nodeid in covering})


def no_coverage_key(mutant: Mutant) -> str:
    # The start byte tells apart two identical mutants on one line (two constants of `return (1, 1)`).
    return sha(f"nocov:{mutant.path}:{mutant.lineno}:{mutant.start_byte}:{mutant.operator}:{mutant.mutated}")


def partition(
    mutants: list[Mutant],
    sources: SourceCache,
    cov: CoverageLike,
    *,
    layout: Layout,
    store: Store | None,
    read_cache: bool,
    memo: FileHashMemo,
    schedule_fuzz: bool = False,
    reexport: ReexportIndex,
    collected_test_nodeids: frozenset[str] = _EMPTY,
    kill_validity: KillValidity | None = None,
    semantic: SemanticPatterns = DEFAULT_SEMANTIC,
) -> tuple[list[Work], ResultsByIndex, int]:
    """-> (work to execute, results resolved without running, cache hits)."""
    work: list[Work] = []
    prefilled: ResultsByIndex = {}
    cache_hits = 0
    identity_cache: IdentityBaseCache = {}
    covered_by_cache: CoveredByCache = {}
    support_hash = support_corpus_hash(layout, memo)
    if kill_validity is None:
        kill_validity = KillValidity(layout, support_hash=support_hash)
    for index, mutant in enumerate(mutants):
        source = sources[mutant.path]
        bucket = classify(mutant)
        module = layout.module_name(mutant.path)
        if isinstance(mutant.fact, SelfCovered):
            # An oracle mutant is covered by its own test (coverage does not cover test files): by qualname.
            identity = identify(
                mutant, source, module=module, schedule_fuzz=schedule_fuzz, identity_cache=identity_cache
            )
            if identity is None:
                prefilled[index] = MutantResult(mutant, MutantStatus.UNSUPPORTED, 0)
                continue
            # The node id comes from the file that actually collects the test: the name-matched defining file or a
            # wrapper re-exporting this very name. A test collected nowhere is unsupported, never a broken node id.
            collect_file = oracle_collect_file(mutant.path, identity["qualname"], reexport, layout=layout)
            if collect_file is None:
                prefilled[index] = MutantResult(mutant, MutantStatus.UNSUPPORTED, 0)
                continue
            covering: AbstractSet[str] = expand_oracle_covering(
                oracle_nodeid(collect_file, identity["qualname"]), collected_test_nodeids
            )
        else:
            covering = cov.tests_covering(mutant.path, mutant.lineno)
            if not covering:
                prefilled[index] = MutantResult(mutant, MutantStatus.NO_COVERAGE, 0, no_coverage_key(mutant))
                if store is not None:
                    persist_no_coverage(store, mutant, bucket, semantic=semantic)  # a test gap too, kept for triage
                continue
            identity = identify(
                mutant, source, module=module, schedule_fuzz=schedule_fuzz, identity_cache=identity_cache
            )
            if identity is None:
                ordered = tuple(sorted(covering))
                prefilled[index] = MutantResult(mutant, MutantStatus.UNSUPPORTED, len(ordered), covering_tests=ordered)
                continue
        covering_sorted = tuple(sorted(covering))
        # Source files the covering tests execute are the mutated function's external dependencies (a helper it
        # calls): their edit changes behaviour under the same tests even when the mutant's own file did not change.
        # The set of a site repeats across its mutants: look it up once per distinct covering set.
        covered = covered_by_cache.get(covering_sorted)
        if covered is None:
            covered = cov.files_covered_by(set(covering))
            covered_by_cache[covering_sorted] = covered
        covered_src = sorted(path for path in covered if layout.is_source_path(path))
        root = layout.root
        cov_hash = covering_hash(
            [
                support_hash,
                _memo_hash(root, mutant.path, memo),
                *(_memo_hash(root, file, memo) for file in covered_src),
                *(_memo_hash(root, file, memo) for file in _covering_files(covering_sorted)),
                # The covering node ids themselves: expanding an oracle base into its parametrized ids changes the set
                # over the same files, and a stale survivor must not outlive that.
                *covering_sorted,
            ]
        )
        # The site's byte offset relative to the function start: unique per site (two identical operators on one
        # line differ by start byte) and stable when the function moves.
        rel_byte = mutant.start_byte - identity["func_start_byte"]
        key = mutant_key(
            mutant.path, identity["qualname"], identity["func_src"], mutant.operator, mutant.mutated, rel_byte
        )
        # A hit needs the key, a matching validity and a resolved verdict: a timeout is always re-run.
        if store is not None and read_cache:
            cached = store.get_verdict(key)
            if (
                cached is not None
                and cached["verdict"] != MutantStatus.TIMEOUT.value
                and cached_verdict_holds(cached, cov_hash, kill_validity, site=(mutant.path, identity["qualname"]))
            ):
                if cached["bucket"] != bucket.value:
                    store.update_mutant_bucket(key, bucket.value)
                prefilled[index] = MutantResult(
                    mutant,
                    MutantStatus(cached["verdict"]),
                    len(covering_sorted),
                    key,
                    covering_sorted,
                    tuple(cached["killed_by"]),
                )
                cache_hits += 1
                continue
        work.append(
            Work(
                index,
                mutant,
                covering_sorted,
                identity["spec"],
                key,
                sha(identity["func_src"]),
                identity["qualname"],
                cov_hash,
                bucket,
            )
        )
    return work, prefilled, cache_hits


def cached_verdict_holds(cached: CachedVerdict, cov_hash: str, kill_validity: KillValidity, *, site: KillDep) -> bool:
    """A kill with recorded dependencies holds while the code its killer actually executed is unchanged.

    The file rule (``cov_hash``) applies to survivors (survival is proven by every covering test) and to kills
    without a record. A record proves the kill only when it names the mutated function ``site`` itself among its
    dependencies (the driver always puts it there): it describes a run of that very function."""
    if cached["verdict"] == MutantStatus.KILLED.value and cached["kill_validity"]:
        deps = kill_deps_decode(cached["kill_deps"])
        if deps is not None and site in deps:
            return kill_validity.current(deps) == cached["kill_validity"]
    return cached["covering_hash"] == cov_hash


def persist_verdict(
    store: Store,
    item: Work,
    result: MutantResult,
    killed_by: list[str],
    *,
    semantic: SemanticPatterns = DEFAULT_SEMANTIC,
) -> None:
    """Write one terminal verdict, incrementally and thread-safely."""
    store.put_verdict(
        key=item.key,
        file=item.mutant.path,
        lineno=item.mutant.lineno,
        operator=item.mutant.operator,
        original=item.mutant.original,
        mutated=item.mutant.mutated,
        qualname=item.qualname,
        func_hash=item.func_hash,
        verdict=result.status.value,
        bucket=item.bucket.value,
        covering=list(item.covering),
        covering_hash=item.covering_hash,
        kind=item.mutant.family,
        killed_by=killed_by,
        semantic=semantic_classes(item.mutant, semantic),
        kill_deps=kill_deps_encode(result.kill_deps) if result.kill_deps and result.kill_validity else "",
        kill_validity=result.kill_validity if result.kill_deps else "",
    )


def persist_no_coverage(
    store: Store, mutant: Mutant, bucket: Bucket, *, semantic: SemanticPatterns = DEFAULT_SEMANTIC
) -> None:
    """A mutant no test covers is a test gap: cached for coverage triage under a lightweight key (never run)."""
    store.put_verdict(
        key=no_coverage_key(mutant),
        file=mutant.path,
        lineno=mutant.lineno,
        operator=mutant.operator,
        original=mutant.original,
        mutated=mutant.mutated,
        qualname="",
        func_hash="",
        verdict=MutantStatus.NO_COVERAGE.value,
        bucket=bucket.value,
        covering=[],
        covering_hash="",
        kind=mutant.family,
        semantic=semantic_classes(mutant, semantic),
    )


def persist_final(store: Store, sources: SourceCache, costs: CostMap, *, layout: Layout) -> None:
    """The final flush: the measured costs and the hashes of the mutated source files (verdicts went in earlier)."""
    if costs:
        store.put_costs(costs)
    for path, source in sources.items():
        if layout.is_source_path(path):
            store.put_file_hash(path, sha(source), "src")
