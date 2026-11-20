"""Mutant identity: the patch target, the content key and the spec a daemon worker applies.

A mutant inside a nested function is patched through its top-level ancestor (the mutated inner code rides that
function's ``co_consts``), so the patch target, the content hash and the byte offset all refer to the ancestor.
Module- and class-level sites are out of reach of the in-memory ``__code__`` path and have no identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from balamut.config import Layout
from balamut.core.hashing import function_source, function_start_lineno
from balamut.core.mutants import ApplyMechanism, Mutant, apply_mutant
from balamut.coverage.discovery import ReexportIndex
from balamut.engine.collection import CollectionError
from balamut.operators.facts import Fuzzable, SelfCovered, mutant_mechanism
from balamut.runtime.bytecode import bytecode_fields
from balamut.runtime.inmemory import find_enclosing_function, inject_yields
from balamut.runtime.plugin import MutantSite, MutantSpec


class Identity(TypedDict):
    qualname: str
    func_src: str
    func_start: int
    func_start_byte: int
    spec: MutantSite
    fuzz_target: str | None  # the function whose await boundaries take schedule-fuzz yield points


@dataclass(frozen=True, slots=True)
class IdentityBase:
    """The part of an identity shared by every mutant of one function."""

    func_src: str
    func_start_byte: int


type IdentityBaseCache = dict[tuple[str, str, int], IdentityBase]


def identify(
    mutant: Mutant,
    source: str,
    *,
    module: str,
    schedule_fuzz: bool = False,
    identity_cache: IdentityBaseCache | None = None,
) -> Identity | None:
    """The qualname (patch target), the function source (content key) and the site; None at module/class level.

    Under ``schedule_fuzz`` the fuzz target of a concurrency mutant is the mutated (possibly nested) function: its
    spec gets yield points at that function's await boundaries."""
    enclosing = find_enclosing_function(source, mutant.lineno)
    if enclosing is None:
        return None
    qualname = enclosing.partition(".<locals>.")[0]
    # The line disambiguates same-named siblings (property getter/setter, overload stubs and the implementation):
    # they share co_qualname, and without it a mutant would be hashed by another body: a stale cache hit.
    func_start = function_start_lineno(source, qualname, lineno=mutant.lineno)
    if func_start is None:
        return None
    cache_key = (mutant.path, qualname, func_start)
    base = identity_cache.get(cache_key) if identity_cache is not None else None
    if base is None:
        func_src = function_source(source, qualname, lineno=mutant.lineno)
        if func_src is None:
            return None
        # The UTF-8 byte offset of the function start, counting lines by '\n' exactly as function_start_lineno does.
        char_offset = sum(len(line) + 1 for line in source.split("\n")[: func_start - 1])
        base = IdentityBase(func_src=func_src, func_start_byte=len(source[:char_offset].encode("utf-8")))
        if identity_cache is not None:
            identity_cache[cache_key] = base
    mechanism = mutant_mechanism(mutant)
    source_bytes = source.encode("utf-8")
    line_start = source_bytes.rfind(b"\n", 0, mutant.start_byte) + 1
    spec: MutantSite = {
        "module": module,
        "qualname": qualname,
        "kind": mutant.family,
        "self_covered": isinstance(mutant.fact, SelfCovered),
        "mechanism": mechanism.value,
        "target_lineno": mutant.lineno,
        "target_col": mutant.start_byte - line_start,
    }
    if mutant.bc is not None:
        bc = bytecode_fields(
            source,
            edit=mutant.bc,
            lineno=mutant.lineno,
            start_byte=mutant.start_byte,
            end_byte=mutant.end_byte,
        )
        if bc is not None:
            spec["bc_kind"] = bc["bc_kind"]
            spec["bc_source"] = bc["bc_source"]
            spec["bc_target"] = bc["bc_target"]
            spec["bc_lineno"] = bc["bc_lineno"]
            spec["bc_col"] = bc["bc_col"]
            spec["bc_end_col"] = bc["bc_end_col"]
            spec["bc_mutated"] = bc["bc_mutated"]
    if mechanism is ApplyMechanism.DEFAULTS:
        # The slot in __defaults__ (or the key in __kwdefaults__) travels structurally on the mutant; the new value
        # is the mutated literal.
        if mutant.default_index is not None:
            spec["default_index"] = mutant.default_index
            spec["default_literal"] = mutant.mutated
        elif mutant.default_name is not None:
            spec["default_name"] = mutant.default_name
            spec["default_literal"] = mutant.mutated
    return {
        "qualname": qualname,
        "func_src": base.func_src,
        "func_start": func_start,
        "func_start_byte": base.func_start_byte,
        "spec": spec,
        "fuzz_target": enclosing if schedule_fuzz and isinstance(mutant.fact, Fuzzable) else None,
    }


def mutant_spec(site: MutantSite, source: str, mutant: Mutant, *, fuzz_target: str | None) -> MutantSpec:
    """The spec a daemon worker applies: ``site`` and the module text ``source`` with ``mutant`` in it, yield points
    at the await boundaries of ``fuzz_target`` included."""
    mutated_source = apply_mutant(source, mutant)
    if fuzz_target is not None:
        mutated_source = inject_yields(mutated_source, fuzz_target)
    return {**site, "mutated_source": mutated_source}


def oracle_nodeid(path: str, qualname: str) -> str:
    """The node id of the test holding an assertion (an oracle mutant is covered by that test itself)."""
    return f"{path}::{qualname.replace('.', '::')}"


def oracle_collect_file(def_file: str, qualname: str, reexport: ReexportIndex, *, layout: Layout) -> str | None:
    """The file whose node ids actually collect the oracle test ``qualname`` of ``def_file``.

    A name-matched test file collects under its own name; a defining file collects only the names a wrapper
    re-exports (by the top-level name: a module-level test or a test class). None: the test collects nowhere."""
    if layout.is_test_file(Path(def_file).name):
        return def_file
    return reexport.get((def_file, qualname.partition(".")[0]))


def expand_oracle_covering(base_nodeid: str, collected: frozenset[str]) -> set[str]:
    """The collected instances of an oracle mutant's test: the node id itself and its parametrized ``[...]`` ids.

    A missing exact match stops the campaign: a bare fallback would yield a false survivor for a parametrized
    test, which a daemon drops as an unknown node id."""
    hits = {nodeid for nodeid in collected if nodeid == base_nodeid or nodeid.startswith(f"{base_nodeid}[")}
    if not hits:
        raise CollectionError(f"the exact oracle node id is missing from the pytest collection: {base_nodeid}")
    return hits


def oracle_collect_files(mutants: list[Mutant], reexport: ReexportIndex) -> frozenset[str]:
    """Files to hand to ``pytest --collect-only`` for the real (parametrized) node ids of oracle covering tests.

    A re-exported test collects under its wrapper's node id, so the wrappers join their defining files: otherwise
    ``wrapper::test[param]`` would be missing from the collection and the campaign would stop on it."""
    def_files = frozenset(m.path for m in mutants if isinstance(m.fact, SelfCovered))
    wrappers = frozenset(wrapper for (def_file, _name), wrapper in reexport.items() if def_file in def_files)
    return def_files | wrappers
