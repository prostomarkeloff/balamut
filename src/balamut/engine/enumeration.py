"""Enumerate the mutants of a campaign: kinds, routing of files to kinds, scope and mechanism filters.

The built-in kinds live in an internal lazy registry (an optional dependency like ``db``/``chain`` is
imported only when its kind is requested); the config file wires everything else: ``KINDS`` registers
or overrides a kind by importing it, ``REMOVE_KINDS`` opts out. A value is an ``OperatorKind``: a tuple
of facts on the capability fold.
"""

from __future__ import annotations

import hashlib
import pickle
import time
from collections import Counter
from collections.abc import Mapping, MutableMapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import cast

from capability import Capability

from balamut.config import KindOverrides, Layout
from balamut.core.mutants import ApplyMechanism, Mutant
from balamut.coverage.discovery import ReexportIndex, reexport_index
from balamut.diff.scope import DiffScope, mutant_in_scope
from balamut.operators.facts import (
    Enumerates,
    OperatorKind,
    SelfCovered,
    Sites,
    effective_mechanism,
    enumerate_facts,
)
from balamut.runtime.inmemory import code_reachable

type SourceCache = dict[str, str]
type PathsByKind = Mapping[str, frozenset[str]]
type ChangedLinesByPath = Mapping[str, tuple[int, ...]]
type PhaseTimings = MutableMapping[str, float]
type KindKinds = dict[str, OperatorKind]


class UnknownKindError(ValueError):
    """A requested kind is neither built-in nor registered by the config file."""


type KindLocation = dict[str, str]  # kind name -> "module:attribute"


# kind name -> "module:attribute" of the built-in kinds, imported lazily per request.
_BUILTIN_KINDS: KindLocation = {
    "src": "balamut.operators.src:src_kind",
    "concurrency": "balamut.operators.concurrency:concurrency_kind",
    "fault": "balamut.operators.fault:fault_kind",
    "oracle": "balamut.operators.oracle:oracle_kind",
    "extreme": "balamut.operators.extreme:extreme_kind",
    "db": "balamut.operators.db:db_kind",
    "chain": "balamut.operators.db:chain_kind",
}


def known_kinds() -> frozenset[str]:
    """Every kind a campaign can run: the built-ins (the config file's KINDS adds its own)."""
    return frozenset(_BUILTIN_KINDS)


def load_builtin_kind(name: str) -> Capability | None:
    """The built-in kind named ``name`` (imported lazily); None for a non-built-in name."""
    location = _BUILTIN_KINDS.get(name)
    if location is None:
        return None
    module_name, attribute = location.split(":", 1)
    return import_module(module_name).__dict__[attribute]


def resolve_kinds(kinds: Sequence[str], overrides: KindOverrides) -> KindKinds:
    """Load every requested kind: the config file's registry wins over the built-ins, and a removed
    kind is unknown by design.

    A kind provides an ``OperatorKind``: a tuple of facts, each implementing ``Sites`` or
    ``Enumerates``.
    """
    plain: KindKinds = {}
    for kind in kinds:
        if kind in overrides.removed:
            raise UnknownKindError(f"unknown mutant kind {kind!r}: the config file removes it")
        loaded = overrides.kinds.get(kind)
        if loaded is None:
            loaded = load_builtin_kind(kind)
        if loaded is None:
            raise UnknownKindError(
                f"unknown mutant kind {kind!r}: it is not built-in and the config file does not register it"
            )
        if not isinstance(loaded, tuple):
            raise UnknownKindError(f"kind {kind!r}: a kind must provide a tuple of operator facts")
        # An untyped registry value narrowed to `tuple`; the element check below fixes the shape.
        facts = cast("OperatorKind", loaded)
        if not all(isinstance(fact, Sites | Enumerates) for fact in facts):
            raise UnknownKindError(f"kind {kind!r}: every fact must implement Sites or Enumerates")
        plain[kind] = facts
    return plain


def _files(root: Path, raw: str) -> list[Path]:
    candidate = root / raw
    return [candidate] if candidate.is_file() else sorted(candidate.rglob("*.py"))


def _phase_add(phase_timings: PhaseTimings | None, phase: str, started: float) -> None:
    if phase_timings is not None:
        phase_timings[phase] = phase_timings.get(phase, 0.0) + (time.perf_counter() - started)


class MutantCache:
    """Content-addressed enumeration cache: the mutants of a file are stored under the hash of its source and the
    kind, so an unchanged file re-enumerates from the pickle instead of re-walking the AST.

    The directory is keyed by the campaign's kind configuration (the caller names it): a changed operator set or
    fact table lands in a fresh directory and never mixes with stale entries."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, rel: str, source_hash: str, kind: str) -> Path:
        key = hashlib.sha256(f"{rel}\0{source_hash}\0{kind}".encode()).hexdigest()
        return self._directory / f"{key}.pickle"

    def load(self, rel: str, source_hash: str, kind: str) -> list[Mutant] | None:
        try:
            raw = self._path(rel, source_hash, kind).read_bytes()
        except OSError:
            return None
        try:
            restored: list[Mutant] = pickle.loads(raw)
        except pickle.PickleError, AttributeError, ImportError, IndexError, ValueError:
            return None
        return restored

    def store(self, rel: str, source_hash: str, kind: str, mutants: list[Mutant]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._path(rel, source_hash, kind)
        temporary = target.with_name(f"{target.name}.tmp")
        temporary.write_bytes(pickle.dumps(mutants))
        temporary.replace(target)


def enumerate_paths(
    paths: list[str],
    kinds: list[str],
    *,
    layout: Layout,
    resolved: KindKinds,
    paths_by_kind: PathsByKind | None = None,
    phase_timings: PhaseTimings | None = None,
    enum_cache: MutantCache | None = None,
) -> tuple[list[Mutant], SourceCache, ReexportIndex]:
    """The mutants of ``paths`` (files or directories, relative to the project root), their sources and the
    re-export index ((defining file, name) -> wrapper).

    ``resolved`` is the outcome of ``resolve_kinds`` for this campaign: per-kind fact tuples, folded per file by
    ``enumerate_facts``. The index is built only for a self-covered run: a self-covered mutant's covering test is
    its own function, collected under its wrapper when re-exported. ``paths_by_kind`` restricts each kind to exact
    files (the diff mode); without it every kind applies to every file."""
    discovery_started = time.perf_counter()
    kind_kinds = resolved
    self_covered_kinds = {
        kind for kind, facts in kind_kinds.items() if any(isinstance(fact, SelfCovered) for fact in facts)
    }
    reexport: ReexportIndex = reexport_index(layout) if self_covered_kinds else {}
    _phase_add(phase_timings, "discovery_reexport", discovery_started)
    enumeration_started = time.perf_counter()
    reexport_def_files = {def_file for def_file, _name in reexport}
    mutants: list[Mutant] = []
    sources: SourceCache = {}
    for raw in paths:
        for file in _files(layout.root, raw):
            rel = file.relative_to(layout.root).as_posix()
            file_kinds = [
                kind for kind in kinds if paths_by_kind is None or rel in paths_by_kind.get(kind, frozenset())
            ]
            if not file_kinds:
                continue
            source = file.read_text(encoding="utf-8")
            sources[rel] = source
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            for kind in file_kinds:
                # A file that neither matches the test patterns nor defines re-exported tests collects nothing:
                # its self-covered mutants would have no node id to run.
                if kind in self_covered_kinds and not layout.is_test_file(file.name) and rel not in reexport_def_files:
                    continue
                if enum_cache is not None:
                    cached = enum_cache.load(rel, source_hash, kind)
                    if cached is not None:
                        mutants.extend(cached)
                        continue
                produced = enumerate_facts(rel, source, (kind_kinds[kind],), family=kind)
                if enum_cache is not None:
                    enum_cache.store(rel, source_hash, kind, produced)
                mutants.extend(produced)
    _phase_add(phase_timings, "mutation_enumeration", enumeration_started)
    return mutants, sources, reexport


def filter_mutants_by_scope(mutants: list[Mutant], scope: DiffScope) -> list[Mutant]:
    """Mutants whose full site span intersects the pinned lines."""
    return [m for m in mutants if mutant_in_scope(m.path, m.site_lineno, m.site_end_lineno, scope.changed_lines)]


def _mechanism(mutant: Mutant, sources: SourceCache) -> ApplyMechanism:
    return effective_mechanism(mutant, code_reachable=code_reachable(sources[mutant.path], mutant.lineno))


def filter_by_mechanism(mutants: list[Mutant], sources: SourceCache, chosen: frozenset[ApplyMechanism]) -> list[Mutant]:
    """Mutants whose effective mechanism (operator plus site reachability) is chosen: ``code`` is the in-memory
    ``__code__`` path (a node inside a function body), ``cold`` is out of its reach (module or class level)."""
    return [mutant for mutant in mutants if _mechanism(mutant, sources) in chosen]


def mechanism_breakdown(mutants: list[Mutant], sources: SourceCache) -> Counter[str]:
    """Mutants by effective mechanism: how many are cold, out of reach of the in-memory path."""
    return Counter(_mechanism(mutant, sources).value for mutant in mutants)
