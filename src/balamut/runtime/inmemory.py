"""In-memory mutation of a live function by swapping its ``__code__`` (no files, no copies).

The module source is recompiled with the mutant applied, the new code object of the target function is taken
by ``co_qualname`` and assigned to ``func.__code__``. The function object stays the same, so every reference,
``from M import f`` included, sees the mutation at once. The mutant lives until the returned restore runs (or
the worker dies); nothing is written to disk.

Top-level functions and methods are patched directly. A nested function (``outer.<locals>.inner``) is patched
through its top-level ancestor, whose ``co_consts`` carries the mutated nested code object. Module-level code
already ran at import time and is unreachable on this path (the ``COLD`` mechanism).
"""

from __future__ import annotations

import ast
import functools
import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from balamut.concurrency import FUZZ_YIELD_NAME
from balamut.core.astcache import parse_module

# The type of an `@lru_cache` wrapper, taken from an instance rather than the private functools name.
LRU_CACHE_WRAPPER = type(functools.lru_cache()(lambda: None))

_LOCALS_MARKER = "<locals>"
# What a namespace lookup yields after unwrapping a method descriptor: some callable, or nothing.
type _Resolvable = Callable[..., Any] | None


@dataclass(frozen=True, slots=True)
class FunctionSpan:
    """Line range of a function and its exact runtime qualname."""

    start: int
    end: int
    depth: int
    qualname: str


def inject_yields(source: str, qualname: str, *, helper: str = FUZZ_YIELD_NAME) -> str:
    """Instrument the await boundaries of the target function only with scheduling points.

    Every ``await E`` becomes ``await <helper>(E, <site>)``, an expression valid in any context; the helper is a
    runtime coroutine that yields according to a seed and then awaits ``E``. Nested functions are untouched."""
    tree = ast.parse(source)
    target = _function_node(tree, qualname)
    if target is None:
        return source
    _YieldInjector(helper).visit(target)
    return ast.unparse(tree)


def _function_node(tree: ast.AST, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    _collect_function_node(tree, [], qualname, found)
    return found[0] if found else None


def _collect_function_node(
    node: ast.AST, stack: list[str], target: str, out: list[ast.FunctionDef | ast.AsyncFunctionDef]
) -> None:
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if ".".join([*stack, child.name]) == target:
                    out.append(child)
                _collect_function_node(child, [*stack, child.name, _LOCALS_MARKER], target, out)
            case ast.ClassDef():
                _collect_function_node(child, [*stack, child.name], target, out)
            case _:
                _collect_function_node(child, stack, target, out)


class _YieldInjector(ast.NodeTransformer):
    """Wraps every ``await E`` of the target function; nested functions are left alone."""

    def __init__(self, helper: str) -> None:
        super().__init__()
        self._helper = helper
        self._site = 0
        self._root = True

    @override
    def visit_Await(self, node: ast.Await) -> ast.AST:
        self.generic_visit(node)  # inner awaits of E first
        self._site += 1
        call = ast.Call(
            func=ast.Name(id=self._helper, ctx=ast.Load()),
            args=[node.value, ast.Constant(value=self._site)],
            keywords=[],
        )
        return ast.Await(value=call)

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._descend_if_root(node)

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._descend_if_root(node)

    def _descend_if_root(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        if not self._root:
            return node
        self._root = False
        self.generic_visit(node)
        return node


def find_enclosing_function(source: str, target_lineno: int) -> str | None:
    """``co_qualname`` of the innermost function whose span contains ``target_lineno``; None at module level."""
    matches = [span for span in enclosing_function_index(source) if span.start <= target_lineno <= span.end]
    if not matches:
        return None
    return max(matches, key=lambda span: (span.depth, span.qualname)).qualname


@functools.cache
def enclosing_function_index(source: str) -> tuple[FunctionSpan, ...]:
    """One memoized function index per module text."""
    spans: list[FunctionSpan] = []
    _collect_function_spans(parse_module(source), [], 0, spans)
    return tuple(spans)


def code_reachable(source: str, lineno: int) -> bool:
    """Can the ``__code__`` runner reach a mutant on ``lineno``? True inside any function body (nested ones are
    patched through their top-level ancestor); False only at module or class level."""
    return find_enclosing_function(source, lineno) is not None


def compile_function_code(source: str, qualname: str, filename: str) -> types.CodeType | None:
    """Compile ``source`` and take the code object of ``qualname``.

    Invalid syntax (an operator spliced broken Python) yields None, so the mutant becomes UNSUPPORTED instead of
    a SyntaxError escaping into the worker and aborting the whole campaign."""
    try:
        module_code = compile(source, filename, "exec")
    except SyntaxError:
        return None
    return _find_code_by_qualname(module_code, qualname)


def patch_code(func: types.FunctionType, new_code: types.CodeType) -> Callable[[], None]:
    """Replace ``func.__code__`` with ``new_code``; return the restore callback."""
    old_code = func.__code__
    func.__code__ = new_code

    def restore() -> None:
        func.__code__ = old_code

    return restore


def _unwrap_to_function[R](obj: Callable[..., R] | None) -> types.FunctionType | None:
    """Unwind the ``__wrapped__`` chain down to the innermost FunctionType, or None.

    Decorators (``@asynccontextmanager``, ``functools.wraps``, ``@lru_cache``) put a wrapper into the module
    whose body is the decorator's (identical for the original and the mutant); the real body is the wrapped
    function. The unwrap must happen before the type check: an ``@lru_cache`` wrapper is not a FunctionType."""
    if not callable(obj):
        return None
    try:
        unwrapped = inspect.unwrap(obj)
    except ValueError:  # a __wrapped__ cycle
        return None
    return unwrapped if isinstance(unwrapped, types.FunctionType) else None


def _noop_cache_clear() -> None:
    """Cache-clear stand-in for functions without an ``@lru_cache`` wrapper."""


def resolve_with_cache_clear(
    module: types.ModuleType, qualname: str
) -> tuple[types.FunctionType | None, Callable[[], None]]:
    """``resolve_function`` plus a callback that clears the wrapper cache (a no-op when there is none).

    The cache is cleared right away: a cache filled at collection or import time would otherwise return the
    old result around the mutated body (a false survivor). The callback is returned so the caller clears it on
    restore too: a persistent worker runs many mutants in one process, and a result cached under mutant A would
    otherwise leak into mutant B through the wrapper."""
    current = module
    owner = ""  # the source name of the class whose namespace is searched; empty at module level
    for part in qualname.split("."):
        if part == _LOCALS_MARKER:
            return None, _noop_cache_clear
        try:
            members = vars(current)
        except TypeError:  # an intermediate name is shadowed by a non-namespace value
            return None, _noop_cache_clear
        key = _mangled(owner, part)
        if key not in members:
            return None, _noop_cache_clear
        current = members[key]
        owner = part
    resolved = _descriptor_target(current)
    cache_clear = _lru_cache_clear(resolved)
    cache_clear()
    return _unwrap_to_function(resolved), cache_clear


def _mangled(owner: str, name: str) -> str:
    """The key under which the body of class ``owner`` binds ``name``.

    ``co_qualname`` keeps the source name (``LRUCache.__touch``), while the compiler stores a private name of a class
    body mangled (``_LRUCache__touch``). A dunder name, a module-level name and a class named only with underscores
    are not mangled."""
    stripped = owner.lstrip("_")
    if not stripped or not name.startswith("__") or name.endswith("__"):
        return name
    return f"_{stripped}{name}"


def _descriptor_target(member: Any) -> _Resolvable:
    """Unwrap a method descriptor (property, staticmethod, classmethod) down to the callable it holds.

    ``member`` comes from ``vars()`` and is untyped by nature. The static/class method test lives in a helper
    without a type guard, so the value is never narrowed into a generic with unknown parameters."""
    if isinstance(member, property):
        return member.fget
    if _is_method_descriptor(member):
        return member.__func__
    return member if callable(member) else None


def _is_method_descriptor(member: Any) -> bool:
    return isinstance(member, staticmethod | classmethod)


def _lru_cache_clear(resolved: _Resolvable) -> Callable[[], None]:
    """The ``cache_clear`` of an ``@lru_cache`` wrapper, or a no-op for anything else."""
    if isinstance(resolved, LRU_CACHE_WRAPPER):
        return resolved.cache_clear
    return _noop_cache_clear


def resolve_function(module: types.ModuleType, qualname: str) -> types.FunctionType | None:
    """The live function object for ``qualname`` (through ``vars``, never ``getattr``).

    Nested functions do not resolve. Descriptors are unwrapped to the function and decorated functions down to
    their body, otherwise the mutation would hit the descriptor or wrapper and silently not apply."""
    return resolve_with_cache_clear(module, qualname)[0]


def _collect_function_spans(node: ast.AST, stack: list[str], depth: int, out: list[FunctionSpan]) -> None:
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                end = child.end_lineno or child.lineno
                out.append(
                    FunctionSpan(start=child.lineno, end=end, depth=depth, qualname=".".join([*stack, child.name]))
                )
                _collect_function_spans(child, [*stack, child.name, _LOCALS_MARKER], depth + 1, out)
            case ast.ClassDef():
                _collect_function_spans(child, [*stack, child.name], depth, out)
            case _:
                _collect_function_spans(child, stack, depth, out)


def _find_code_by_qualname(code: types.CodeType, qualname: str) -> types.CodeType | None:
    """The unique code object with ``co_qualname == qualname``, otherwise None.

    Ambiguity (a repeated top-level ``def``, ``@overload`` stubs, a property getter and setter) cannot be
    resolved by qualname: the live object is the last binding, and grafting a first match would install a
    different definition's body. Refusing yields UNSUPPORTED rather than a silent false verdict."""
    matches = _collect_code_by_qualname(code, qualname)
    return matches[0] if len(matches) == 1 else None


def _collect_code_by_qualname(code: types.CodeType, qualname: str) -> list[types.CodeType]:
    found: list[types.CodeType] = []
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            if const.co_qualname == qualname:
                found.append(const)
            found.extend(_collect_code_by_qualname(const, qualname))
    return found
