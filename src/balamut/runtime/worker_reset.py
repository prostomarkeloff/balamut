"""Reset the safe part of a persistent worker's process state at a mutant boundary.

A persistent worker reuses one process for many mutants, so global state leaks between them where a fresh
fork would reset it. Only caches are reverted:

- ``@lru_cache`` wrappers: a cache would otherwise return a previous mutant's values (directly or through the
  mutated function), bypassing the code restore. ``cache_clear`` only empties a dict, which is safe.

``sys.modules`` is never touched. Re-importing a C extension can run ``PyInit_`` again over process-global
state and crash; partially unloading a pure-Python graph breaks identity (a re-imported module may attach the
old singleton, router or registry from a module that stayed). The remaining state is controlled by touch and
paired confirmation: a kill whose mutated code really ran is confirmed by the original passing in the same
process state, and a kill or survivor without a touch is re-run in a fresh fork on a clean database.

Residual limits (only a fresh fork closes them):

- a test rebinding a module-level name of an already loaded module (a counter, a registry, a singleton) or
  mutating such an object in place. A ``__dict__`` snapshot/restore would fix it but costs about 40% of wall
  time per mutant, for a vector that has not been observed to change a verdict;
- a test mutating process-global state of a C extension (pools, terminal) — session fixtures, in practice.
"""

from __future__ import annotations

import gc
import sys
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import Any, NamedTuple, Protocol, cast

from balamut.runtime.inmemory import LRU_CACHE_WRAPPER


class WorkerBaseline(NamedTuple):
    """The worker's state before its first mutant (after collection): the reference for every boundary.

    ``modules`` are the ``sys.modules`` keys at start (a target module outside them was imported lazily and
    the mutant is UNSUPPORTED). ``cache_clears`` are the ``cache_clear`` callbacks of the ``@lru_cache``
    wrappers ``capture_baseline`` reaches."""

    modules: frozenset[str]
    cache_clears: tuple[Callable[[], None], ...]


class CacheWrapper(Protocol):
    """The part of an ``@lru_cache`` wrapper a cache reset reads."""

    @property
    def __wrapped__(self) -> Callable[..., object]: ...

    def cache_clear(self) -> None: ...


def capture_baseline() -> WorkerBaseline:
    """Snapshot the module set and the ``@lru_cache`` wrappers of the process. Called once, before the first
    mutant is applied.

    ``gc`` finds the wrappers created after the fork, module, class and nested ones at once. The daemon's
    collection heap is frozen before the fork and invisible to ``gc``: its wrappers are found through the module
    namespaces (``namespace_cache_wrappers``)."""
    cache_clears = {id(obj): obj.cache_clear for obj in gc.get_objects() if type(obj) is LRU_CACHE_WRAPPER}
    for wrapper in namespace_cache_wrappers(plain_modules()):
        cache_clears.setdefault(id(wrapper), wrapper.cache_clear)
    return WorkerBaseline(frozenset(sys.modules), tuple(cache_clears.values()))


def plain_modules() -> tuple[ModuleType, ...]:
    """The imported modules whose namespace reads have no side effects: exactly ``ModuleType``. A lazy module
    subclass imports its target on first attribute access."""
    return tuple(module for module in tuple(sys.modules.values()) if type(module) is ModuleType)


def namespace_cache_wrappers(modules: Iterable[ModuleType]) -> tuple[CacheWrapper, ...]:
    """The ``@lru_cache`` wrappers bound in the namespaces of ``modules`` and of the classes each defines (nested
    classes included): as a function, a property getter, a static or a class method.

    This reaches the wrappers ``gc.get_objects`` cannot: pytest-fast freezes the collected heap before the fork,
    and frozen objects are not listed. Only exact types are tested: a namespace can hold a lazy proxy whose
    ``__class__`` evaluates the object it stands for, which an ``isinstance`` call would trigger. A wrapper held
    only in a closure cell (a decorator that keeps the cache inside) stays out of reach."""
    found: dict[int, CacheWrapper] = {}
    classes: set[int] = set()

    def walk(namespace: Mapping[str, Any], module_name: str) -> None:
        for member in tuple(namespace.values()):
            if issubclass(type(member), type):
                if member.__module__ == module_name and id(member) not in classes:
                    classes.add(id(member))
                    walk(cast("type", member).__dict__, module_name)
                continue
            wrapper = _held_cache_wrapper(member)
            if wrapper is not None:
                found[id(wrapper)] = wrapper

    for module in modules:
        walk(module.__dict__, module.__name__)
    return tuple(found.values())


def _held_cache_wrapper(member: Any) -> CacheWrapper | None:
    """The ``@lru_cache`` wrapper a namespace member binds: itself, a property getter, a static or class method."""
    if issubclass(type(member), property):
        member = member.fget
    elif issubclass(type(member), staticmethod | classmethod):
        member = member.__func__
    return cast("CacheWrapper", member) if type(member) is LRU_CACHE_WRAPPER else None


def reset_to_baseline(baseline: WorkerBaseline) -> None:
    """Clear the baseline ``@lru_cache`` wrappers without changing the import graph."""
    for cache_clear in baseline.cache_clears:
        cache_clear()
