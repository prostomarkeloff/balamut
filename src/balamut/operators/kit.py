"""The operator kit: composable Lego pieces for authoring facts.

Matchers are frozen predicates over an AST node and its context, combinable with ``&``, ``|``
and ``~``. Facts are assembled from a matcher plus a replacement strategy: ``Replace`` splices
fixed text over every matched node, ``Rewrite`` splices per-site text from a site finder, and
``RegionRewrite`` is a whole-file phase rewriting one whole region per matched node (with the
kit's indent helpers, so multi-line replacements stay valid). Everything is data: a fact built
by the kit is an ordinary ``Sites``/``Enumerates`` value, and the engine's fold needs nothing
new.
"""

from __future__ import annotations

import ast
import copy
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from balamut.core.mutants import BcEdit, BcEditKind, Mutant, is_none_constant, non_body_node_ids
from balamut.core.source_spans import line_byte_starts, node_byte_span
from balamut.operators.facts import EnumContext


class Library(Enum):
    """The standard libraries of operator names (``lib:area:op``).

    The areas (``str``, ``builtins``, ``session``, ...) are plain strings: the enum holds the
    library level only, and a custom library (or a project-domain namespace) is a plain string
    wherever the enum is accepted."""

    LANG = "lang"  # core syntax: compare/binop/const/control/return/delete
    STD = "std"  # the standard library: str, decimal, datetime, asyncio, threading, re, ...
    SA = "sa"  # SQLAlchemy: query chains and session methods
    PYTEST = "pytest"  # pytest: raises


def operator_name(namespace: Library | str, *parts: str) -> str:
    """``lib:area:op`` from a library (an enum member or a custom string) and its parts."""
    prefix = namespace.value if isinstance(namespace, Library) else namespace
    return ":".join((prefix, *parts))


def context(path: str, source: str, *, tree: ast.Module | None = None) -> EnumContext:
    """A kit-built enumeration context (with the parent map) for whole-file phases.

    ``tree`` is the caller's own parse when it already has one: the parent map must hold the ids of
    the very tree the caller walks (a separate parse would have different ids).
    """
    tree = tree if tree is not None else ast.parse(source)
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return EnumContext(
        path,
        source.encode("utf-8"),
        line_byte_starts(source),
        non_body_node_ids(tree),
        parents=parents,
    )


# --- Matchers ---------------------------------------------------------------


class Matcher(ABC):
    """A frozen predicate over a node and its context; combinable with ``&``, ``|``, ``~``."""

    @abstractmethod
    def matches(self, node: ast.AST, ctx: EnumContext) -> bool: ...

    def __and__(self, other: Matcher) -> Matcher:
        return And(self, other)

    def __or__(self, other: Matcher) -> Matcher:
        return Or(self, other)

    def __invert__(self) -> Matcher:
        return Not(self)


@dataclass(frozen=True, slots=True)
class And(Matcher):
    left: Matcher
    right: Matcher

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return self.left.matches(node, ctx) and self.right.matches(node, ctx)


@dataclass(frozen=True, slots=True)
class Or(Matcher):
    left: Matcher
    right: Matcher

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return self.left.matches(node, ctx) or self.right.matches(node, ctx)


@dataclass(frozen=True, slots=True)
class Not(Matcher):
    inner: Matcher

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return not self.inner.matches(node, ctx)


@dataclass(frozen=True, slots=True)
class NodeIs(Matcher):
    """The node is an instance of one of the given AST types."""

    types: tuple[type[ast.AST], ...]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, self.types)


def node_is(*types: type[ast.AST]) -> NodeIs:
    return NodeIs(types)


@dataclass(frozen=True, slots=True)
class Named(Matcher):
    """An ``ast.Name`` whose id is one of the given names."""

    names: frozenset[str]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, ast.Name) and node.id in self.names


def named(*names: str) -> Named:
    return Named(frozenset(names))


@dataclass(frozen=True, slots=True)
class AttributeOf(Matcher):
    """An ``ast.Attribute`` whose attribute name is one of the given names."""

    attrs: frozenset[str]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, ast.Attribute) and node.attr in self.attrs


def attribute(*attrs: str) -> AttributeOf:
    return AttributeOf(frozenset(attrs))


@dataclass(frozen=True, slots=True)
class NonEmptyCollection(Matcher):
    """A list/dict/set/tuple literal with at least one element."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        match node:
            case ast.List(elts=elts) | ast.Set(elts=elts) | ast.Tuple(elts=elts):
                return bool(elts)
            case ast.Dict(keys=keys):
                return bool(keys)
            case _:
                return False


def non_empty_collection() -> NonEmptyCollection:
    return NonEmptyCollection()


@dataclass(frozen=True, slots=True)
class CallOf(Matcher):
    """An ``ast.Call`` to the named function and/or a method of one of ``attrs``, with at least
    ``min_args`` positional arguments. The ``receiver`` constrains the callee's base name."""

    func: str | None = None
    attr: str | frozenset[str] | None = None
    receiver: str | None = None
    min_args: int = 0

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if not isinstance(node, ast.Call):
            return False
        callee = node.func
        if self.func is not None and (not isinstance(callee, ast.Name) or callee.id != self.func):
            return False
        if self.receiver is not None:
            if not isinstance(callee, ast.Attribute) or not isinstance(callee.value, ast.Name):
                return False
            if callee.value.id != self.receiver:
                return False
        if self.attr is not None:
            if not isinstance(callee, ast.Attribute):
                return False
            attrs = {self.attr} if isinstance(self.attr, str) else self.attr
            if callee.attr not in attrs:
                return False
        return len(node.args) >= self.min_args


def call_of(
    func: str | None = None,
    *,
    attr: str | frozenset[str] | None = None,
    receiver: str | None = None,
    min_args: int = 0,
) -> CallOf:
    return CallOf(func=func, attr=attr, receiver=receiver, min_args=min_args)


type ConstantType = type[str | int | float | bool]


@dataclass(frozen=True, slots=True)
class ConstOf(Matcher):
    """An ``ast.Constant`` of ``type_`` (any type when None); ``non_empty`` for filled strings;
    ``values`` restricts to the given literals."""

    type_: ConstantType | None = None
    non_empty: bool = False
    values: tuple[str | int | float | bool, ...] | None = None

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if not isinstance(node, ast.Constant):
            return False
        value = node.value
        if self.type_ is not None and not isinstance(value, self.type_):
            return False
        if self.non_empty and not value:
            return False
        return not (self.values is not None and value not in self.values)


def const_of(
    type_: ConstantType | None = None,
    *,
    non_empty: bool = False,
    values: tuple[str | int | float | bool, ...] | None = None,
) -> ConstOf:
    return ConstOf(type_=type_, non_empty=non_empty, values=values)


@dataclass(frozen=True, slots=True)
class ResultIsUsed(Matcher):
    """The node's parent consumes its value (assignment, return, argument, operand, condition)."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        parent = ctx.parent_of(node)
        match parent:
            case ast.Assign(value=value) | ast.AnnAssign(value=value):
                return value is node
            case ast.Return(value=value):
                return value is node
            case ast.Call():
                return any(argument is node for argument in parent.args) or any(
                    keyword.value is node for keyword in parent.keywords
                )
            case ast.BinOp(left=left, right=right):
                return left is node or right is node
            case ast.If(test=test) | ast.While(test=test):
                return test is node
            case _:
                return False


def result_is_used() -> ResultIsUsed:
    return ResultIsUsed()


@dataclass(frozen=True, slots=True)
class Inside(Matcher):
    """Some ancestor of the node is an instance of one of the given AST types."""

    types: tuple[type[ast.AST], ...]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        parent = ctx.parent_of(node)
        while parent is not None:
            if isinstance(parent, self.types):
                return True
            parent = ctx.parent_of(parent)
        return False


def inside(*types: type[ast.AST]) -> Inside:
    return Inside(types)


@dataclass(frozen=True, slots=True)
class FirstInSuite(Matcher):
    """The node is the first statement of its suite."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return ctx.suite_index(node) == 0


def first_in_suite() -> FirstInSuite:
    return FirstInSuite()


@dataclass(frozen=True, slots=True)
class LastInSuite(Matcher):
    """The node is the last statement of its suite."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        suite = ctx.suite_of(node)
        return suite is not None and suite[-1] is node


def last_in_suite() -> LastInSuite:
    return LastInSuite()


@dataclass(frozen=True, slots=True)
class AssignmentTarget(Matcher):
    """The node is (part of) the target of an assignment."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        parent = ctx.parent_of(node)
        if parent is None:
            return False
        match parent:
            case ast.Assign(targets=targets):
                return any(_contains(target, node) for target in targets)
            case ast.AnnAssign(target=target) | ast.AugAssign(target=target):
                return _contains(target, node)
            case _:
                return False


def _contains(root: ast.AST, node: ast.AST) -> bool:
    return any(child is node for child in ast.walk(root))


def assignment_target() -> AssignmentTarget:
    return AssignmentTarget()


@dataclass(frozen=True, slots=True)
class NodeInSet(Matcher):
    """The node's id is in the given set (per-parse ids from a prepared analysis)."""

    ids: frozenset[int]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return id(node) in self.ids


def in_ids(ids: frozenset[int]) -> NodeInSet:
    return NodeInSet(ids)


@dataclass(frozen=True, slots=True)
class ValueOf(Matcher):
    """The node's value matches ``inner``: for an expression statement its ``.value``, unwrapping
    one ``Await`` when ``through_await``; for anything else the node itself."""

    inner: Matcher
    through_await: bool = True

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        value = node.value if isinstance(node, ast.Expr) else node
        if self.through_await and isinstance(value, ast.Await):
            value = value.value
        return self.inner.matches(value, ctx)


def value_of(inner: Matcher, *, through_await: bool = True) -> ValueOf:
    return ValueOf(inner, through_await=through_await)


def expr_value(inner: Matcher) -> Matcher:
    """An expression statement whose value (through one Await) matches ``inner`` — a composition."""
    return node_is(ast.Expr) & value_of(inner)


# --- Site finders ------------------------------------------------------------


@runtime_checkable
class SiteFinder(Protocol):
    """(target, replacement text, splice span override, operator override or None, bytecode edit or None) over
    one matched host node. The operator override lets one table-driven finder name each swap; the fact's own
    ``operator`` is the fallback. The bytecode edit is a structured size-preserving edit the finder declares."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]: ...


def _atomic(text: str, node: ast.expr) -> str:
    """Parenthesize a replacement that is not atomic (mirrors the engine's operand rule)."""
    if isinstance(node, ast.Name | ast.Attribute | ast.Subscript | ast.Call):
        return text
    return f"({text})"


@dataclass(frozen=True, slots=True)
class CallToArgument(SiteFinder):
    """(the call, the atomic text of one of its arguments): PIT-style argument propagation."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call):
            for argument in node.args:
                if not isinstance(argument, ast.Starred):
                    yield node, _atomic(ast.unparse(argument), argument), None, None, None


def attr_name_span(func: ast.Attribute, ctx: EnumContext) -> tuple[int, int] | None:
    """The byte span of just the attribute name (the Attribute node spans the whole expression)."""
    if func.end_lineno is None or func.end_col_offset is None:
        return None
    end = ctx.line_starts[func.end_lineno - 1] + func.end_col_offset
    start = end - len(func.attr.encode("utf-8"))
    return start, end


@dataclass(frozen=True, slots=True)
class AttrSwap(SiteFinder):
    """(the callee attribute, the swapped name) when the attribute is in the swap table; the splice
    covers only the attribute name, keeping the receiver."""

    swaps: tuple[tuple[str, str], ...]

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for name, swapped in self.swaps:
                if node.func.attr == name:
                    yield node.func, swapped, attr_name_span(node.func, ctx), None, None


@dataclass(frozen=True, slots=True)
class EmptyCollection(SiteFinder):
    """(the collection, the empty literal of its kind) — emptiness is the matcher's job
    (``non_empty_collection``), the finder only maps the kind to its empty form."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        match node:
            case ast.List():
                yield node, "[]", None, None, None
            case ast.Dict():
                yield node, "{}", None, None, None
            case ast.Set():
                yield node, "set()", None, None, None
            case _:
                pass


@dataclass(frozen=True, slots=True)
class SetFirstLiteralArg(SiteFinder):
    """The first literal argument of the call becomes ``text`` (bounds, limits, backoff).

    The shape of the host call is the matcher's job; this piece only owns the replacement —
    it fits sleep/wait/join-style timeouts and any other call whose literal argument the author
    wants rewritten.
    """

    text: str
    index: int = 0

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) <= self.index:
            return
        argument = node.args[self.index]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, int | float):
            yield argument, self.text, None, None, None


@dataclass(frozen=True, slots=True)
class RegexRuleSite(SiteFinder):
    """(the pattern literal, the rewritten text) per matched rule — weapon-regex level-1 style.

    The rules are (needle, replacement) string pairs; each applies at most once per pattern.
    The pattern sits at positional argument ``index`` of the call.
    """

    rules: tuple[tuple[str, str], ...]
    index: int = 0
    _compiled: tuple[tuple[re.Pattern[str], str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_compiled", tuple((re.compile(needle), replacement) for needle, replacement in self.rules)
        )

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) <= self.index:
            return
        pattern = node.args[self.index]
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
            return
        for rule, replacement in self._compiled:
            if rule.search(pattern.value):
                yield pattern, repr(rule.sub(replacement, pattern.value, count=1)), None, None, None


@dataclass(frozen=True, slots=True)
class DropArgument(SiteFinder):
    """Drops the positional argument at ``index``; the separator goes with it."""

    index: int

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) <= self.index:
            return
        spans = [node_byte_span(argument, ctx.line_starts) for argument in node.args]
        concrete = [span for span in spans if span is not None]
        if len(concrete) != len(spans):
            return
        if self.index < len(concrete) - 1:
            start, end = concrete[self.index][0], concrete[self.index + 1][0]
        else:
            start, end = concrete[self.index - 1][1], concrete[self.index][1]
        yield node.args[self.index], "", (start, end), None, None


def _separator_before(ctx: EnumContext, name_start: int) -> int | None:
    """The byte of the comma right before the keyword (skipping whitespace), or None."""
    position = name_start - 1
    while position >= 0 and ctx.source_bytes[position] in b" \t":
        position -= 1
    return position if position >= 0 and ctx.source_bytes[position] == 0x2C else None


def _previous_argument_end(node: ast.Call, index: int, ctx: EnumContext) -> int | None:
    if index > 0:
        span = node_byte_span(node.keywords[index - 1].value, ctx.line_starts)
        return span[1] if span is not None else None
    if node.args:
        span = node_byte_span(node.args[-1], ctx.line_starts)
        return span[1] if span is not None else None
    return None


def _next_argument_start(node: ast.Call, index: int, ctx: EnumContext) -> int | None:
    if index + 1 < len(node.keywords):
        keyword = node.keywords[index + 1]
        return ctx.line_starts[keyword.lineno - 1] + keyword.col_offset
    if node.args:
        span = node_byte_span(node.args[0], ctx.line_starts)
        return span[0] if span is not None else None
    return None


@dataclass(frozen=True, slots=True)
class DropKeyword(SiteFinder):
    """Drops the keyword argument ``name=...`` of a call; one separator goes with it."""

    name: str

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call):
            return
        for index, keyword in enumerate(node.keywords):
            if keyword.arg != self.name:
                continue
            value_span = node_byte_span(keyword.value, ctx.line_starts)
            if value_span is None:
                continue
            name_start = ctx.line_starts[keyword.lineno - 1] + keyword.col_offset
            previous_end = _previous_argument_end(node, index, ctx)
            if previous_end is not None:
                # `f(a, name=x)` -> `f(a)`: the comma and whitespace before the keyword go away.
                start = _separator_before(ctx, name_start)
                if start is not None:
                    yield keyword.value, "", (start, value_span[1]), None, None
                continue
            next_start = _next_argument_start(node, index, ctx)
            if next_start is not None:
                # `f(name=x, b)` -> `f(b)`: the keyword, its value and the separator go away.
                yield keyword.value, "", (name_start, next_start), None, None


@dataclass(frozen=True, slots=True)
class ScaleLiteral(SiteFinder):
    """(the integer literal at argument ``index``, its value scaled by ``scale``)."""

    scale: int
    index: int = 1

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) <= self.index:
            return
        argument = node.args[self.index]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
            yield argument, str(argument.value * self.scale), None, None, None


@dataclass(frozen=True, slots=True)
class ShiftNumber(SiteFinder):
    """(the integer constant, its value shifted by ``delta``). Booleans are the host of FlipBool."""

    delta: int

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            yield node, str(node.value + self.delta), None, None, None


@dataclass(frozen=True, slots=True)
class FlipBool(SiteFinder):
    """(the boolean constant, its negation)."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            yield node, str(not node.value), None, None, None


@dataclass(frozen=True, slots=True)
class BinaryOperand(SiteFinder):
    """PIT AOD: the binary operation becomes one of its members (parenthesized when needed)."""

    side: Literal["left", "right"]

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.BinOp):
            return
        operand = node.left if self.side == "left" else node.right
        yield node, _atomic(ast.unparse(operand), operand), None, None, None


@dataclass(frozen=True, slots=True)
class RemoveAwait(SiteFinder):
    """The forgotten-await mutation: ``await x`` becomes ``x``."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Await):
            yield node, ast.unparse(node.value), None, None, None


@dataclass(frozen=True, slots=True)
class UnaryOperand(SiteFinder):
    """PIT UOI / Stryker Unary: the unary operation becomes its operand."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.UnaryOp):
            yield node, _atomic(ast.unparse(node.operand), node.operand), None, None, None


@dataclass(frozen=True, slots=True)
class SwapCallArgs(SiteFinder):
    """Bradbury ESP: exchange two positional arguments of a call (a whole-call rewrite)."""

    first: int
    second: int

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) <= max(self.first, self.second):
            return
        swapped = copy.deepcopy(node)
        swapped.args[self.first], swapped.args[self.second] = node.args[self.second], node.args[self.first]
        yield node, ast.unparse(swapped), None, None, None


@dataclass(frozen=True, slots=True)
class SwapAdjacentArgs(SiteFinder):
    """Every adjacent pair of positional arguments of the call, exchanged (a whole-call rewrite).

    An identical pair (``f(a, a)``) is skipped: the swap changes nothing."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            return
        for index in range(len(node.args) - 1):
            left, right = node.args[index], node.args[index + 1]
            if ast.unparse(left) == ast.unparse(right):
                continue
            swapped = copy.deepcopy(node)
            swapped.args[index], swapped.args[index + 1] = right, left
            yield node, ast.unparse(swapped), None, None, None


@dataclass(frozen=True, slots=True)
class NakedReceiver(SiteFinder):
    """PIT EXPERIMENTAL_NAKED_RECEIVER: ``a.b(...)`` becomes its receiver ``a``."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node, _atomic(ast.unparse(node.func.value), node.func.value), None, None, None


@dataclass(frozen=True, slots=True)
class UnaryOpOf(Matcher):
    """A ``UnaryOp`` whose operator is an instance of ``op``."""

    op: type[ast.unaryop]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, ast.UnaryOp) and isinstance(node.op, self.op)


def unary_op(op: type[ast.unaryop]) -> UnaryOpOf:
    return UnaryOpOf(op)


@dataclass(frozen=True, slots=True)
class ReturnHasValue(Matcher):
    """A return whose value exists and is not the None constant."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, ast.Return) and node.value is not None and not is_none_constant(node.value)


def return_has_value() -> ReturnHasValue:
    return ReturnHasValue()


# --- Finders for the src kind's tables ----------------------------------------


def _namespace(namespace: Library | str) -> str:
    """The plain string of a library namespace (an enum member or a custom string)."""
    return namespace.value if isinstance(namespace, Library) else namespace


def _is_size(node: ast.expr) -> bool:
    """A call whose result is never negative: ``len(...)`` or ``abs(...)``."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"len", "abs"}


def _is_zero(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 0


# Swaps that keep the value of a comparison between a size and zero (``len(x) == 0`` is ``len(x) <= 0``), keyed by
# the side the size stands on; the equivalent-mutant heuristic of Petrović et al. (TSE 2021) for collection sizes.
_SIZE_EQUIVALENT: dict[str, frozenset[tuple[type[ast.cmpop], type[ast.cmpop]]]] = {
    "left": frozenset({(ast.Eq, ast.LtE), (ast.LtE, ast.Eq), (ast.NotEq, ast.Gt), (ast.Gt, ast.NotEq)}),
    "right": frozenset({(ast.Eq, ast.GtE), (ast.GtE, ast.Eq), (ast.NotEq, ast.Lt), (ast.Lt, ast.NotEq)}),
}


def _size_equivalent(left: ast.expr, right: ast.expr, swap: tuple[type[ast.cmpop], type[ast.cmpop]]) -> bool:
    if _is_size(left) and _is_zero(right):
        return swap in _SIZE_EQUIVALENT["left"]
    if _is_zero(left) and _is_size(right):
        return swap in _SIZE_EQUIVALENT["right"]
    return False


@dataclass(frozen=True, slots=True)
class CompareSwapSite(SiteFinder):
    """Each swappable op of a compare chain, rebuilt with the table's counterpart; a swap that cannot change a
    comparison of a size with zero is skipped."""

    swaps: tuple[tuple[type[ast.cmpop], type[ast.cmpop]], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Compare):
            return
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            for from_, to in self.swaps:
                if type(op) is from_ and not _size_equivalent(operands[index], operands[index + 1], (from_, to)):
                    mutated = copy.deepcopy(node)
                    mutated.ops[index] = to()
                    bc = BcEdit(BcEditKind.COMPARE, from_.__name__, to.__name__, "")
                    name = operator_name(_namespace(self.namespace), "compare", f"{from_.__name__}->{to.__name__}")
                    yield node, ast.unparse(mutated), None, name, bc


@dataclass(frozen=True, slots=True)
class BinopSwapSite(SiteFinder):
    """The swapped BinOp, rebuilt from the table."""

    swaps: tuple[tuple[type[ast.operator], type[ast.operator]], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.BinOp):
            return
        for from_, to in self.swaps:
            if type(node.op) is from_:
                mutated = copy.deepcopy(node)
                mutated.op = to()
                bc = BcEdit(BcEditKind.BINOP, from_.__name__, to.__name__, "")
                name = operator_name(_namespace(self.namespace), "binop", f"{from_.__name__}->{to.__name__}")
                yield node, ast.unparse(mutated), None, name, bc


@dataclass(frozen=True, slots=True)
class AugAssignSwapSite(SiteFinder):
    """The swapped AugAssign, rebuilt from the table."""

    swaps: tuple[tuple[type[ast.operator], type[ast.operator]], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.AugAssign):
            return
        for from_, to in self.swaps:
            if type(node.op) is from_:
                mutated = copy.deepcopy(node)
                mutated.op = to()
                name = operator_name(_namespace(self.namespace), "augassign", f"{from_.__name__}->{to.__name__}")
                yield node, ast.unparse(mutated), None, name, None


@dataclass(frozen=True, slots=True)
class BoolopSwapSite(SiteFinder):
    """The swapped BoolOp, rebuilt from the table."""

    swaps: tuple[tuple[type[ast.boolop], type[ast.boolop]], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.BoolOp):
            return
        for from_, to in self.swaps:
            if type(node.op) is from_:
                mutated = copy.deepcopy(node)
                mutated.op = to()
                name = operator_name(_namespace(self.namespace), "boolop", f"{from_.__name__}->{to.__name__}")
                yield node, ast.unparse(mutated), None, name, None


@dataclass(frozen=True, slots=True)
class CalleeSwapSite(SiteFinder):
    """The call with its named callee swapped (direction pairs)."""

    swaps: tuple[tuple[str, str], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            for from_, to in self.swaps:
                if node.func.id == from_:
                    mutated = copy.deepcopy(node)
                    callee = mutated.func
                    if isinstance(callee, ast.Name):
                        callee.id = to
                        name = operator_name(_namespace(self.namespace), f"{from_}->{to}")
                        yield node, ast.unparse(mutated), None, name, None


@dataclass(frozen=True, slots=True)
class MethodSwapSite(SiteFinder):
    """The call with its method name swapped (known directional pairs)."""

    swaps: tuple[tuple[str, str], ...]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for from_, to in self.swaps:
                if node.func.attr == from_:
                    mutated = copy.deepcopy(node)
                    callee = mutated.func
                    if isinstance(callee, ast.Attribute):
                        callee.attr = to
                        name = operator_name(_namespace(self.namespace), f"{from_}->{to}")
                        yield node, ast.unparse(mutated), None, name, None


@dataclass(frozen=True, slots=True)
class CalleeDropSite(SiteFinder):
    """The call returns its own operand rounded: the operand stands in (atomic text)."""

    names: frozenset[str]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.names and node.args:
            operand = node.args[0]
            if not isinstance(operand, ast.Starred):
                name = operator_name(_namespace(self.namespace), f"{node.func.id}-drop")
                yield node, _atomic(ast.unparse(operand), operand), None, name, None


@dataclass(frozen=True, slots=True)
class MethodDropSite(SiteFinder):
    """The method call returns its receiver rounded: the receiver stands in (atomic text)."""

    attrs: frozenset[str]
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in self.attrs:
            receiver = node.func.value
            name = operator_name(_namespace(self.namespace), f"{node.func.attr}-drop")
            yield node, _atomic(ast.unparse(receiver), receiver), None, name, None


@dataclass(frozen=True, slots=True)
class NumberShiftSite(SiteFinder):
    """The numeric constant shifted by ``delta``, parenthesized before a following dot
    (``0xff.real`` -> ``(256).real``)."""

    delta: int
    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            return
        if not isinstance(node.value, int | float):
            return
        mutated = str(node.value + self.delta)
        span = node_byte_span(node, ctx.line_starts)
        if span is not None and span[1] < len(ctx.source_bytes) and ctx.source_bytes[span[1]] == 0x2E:
            mutated = f"({mutated})"
        bc = BcEdit(BcEditKind.CONST, "", "", mutated)
        name = operator_name(_namespace(self.namespace), "const", f"num:{node.value}->{node.value + self.delta}")
        yield node, mutated, None, name, bc


@dataclass(frozen=True, slots=True)
class FlipBoolSite(SiteFinder):
    """The boolean constant negated."""

    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            text = str(not node.value)
            bc = BcEdit(BcEditKind.CONST, "", "", text)
            name = operator_name(_namespace(self.namespace), "const", f"bool:{node.value}->{not node.value}")
            yield node, text, None, name, bc


@dataclass(frozen=True, slots=True)
class DropEachArgument(SiteFinder):
    """Every positional argument of the call, dropped with one separator."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            return
        spans = [node_byte_span(argument, ctx.line_starts) for argument in node.args]
        concrete = [span for span in spans if span is not None]
        if len(concrete) != len(spans):
            return
        for index, argument in enumerate(node.args):
            if index < len(concrete) - 1:
                start, end = concrete[index][0], concrete[index + 1][0]
            else:
                start, end = concrete[index - 1][1], concrete[index][1]
            yield argument, "", (start, end), None, None


@dataclass(frozen=True, slots=True)
class DropEachKeyword(SiteFinder):
    """Every keyword argument of the call, dropped with one separator (``f(x, k=v)`` -> ``f(x)``).

    ``**mapping`` splats (``keyword.arg`` is None) are left alone."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call) or not node.keywords:
            return
        for index, keyword in enumerate(node.keywords):
            if keyword.arg is None:
                continue
            value_span = node_byte_span(keyword.value, ctx.line_starts)
            if value_span is None:
                continue
            name_start = ctx.line_starts[keyword.lineno - 1] + keyword.col_offset
            previous_end = _previous_argument_end(node, index, ctx)
            if previous_end is not None:
                start = _separator_before(ctx, name_start)
                if start is not None:
                    yield keyword.value, "", (start, value_span[1]), None, None
                continue
            next_start = _next_argument_start(node, index, ctx)
            if next_start is not None:
                yield keyword.value, "", (name_start, next_start), None, None


@dataclass(frozen=True, slots=True)
class DropEachMappingSplat(SiteFinder):
    """Every ``**mapping`` argument of the call, dropped with one separator (``f(x, **kw)`` -> ``f(x)``,
    ``f(**kw)`` -> ``f()``): the forwarded options a wrapper loses."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call):
            return
        for index, keyword in enumerate(node.keywords):
            if keyword.arg is not None:
                continue
            value_span = node_byte_span(keyword.value, ctx.line_starts)
            if value_span is None:
                continue
            splat_start = ctx.line_starts[keyword.lineno - 1] + keyword.col_offset
            if _previous_argument_end(node, index, ctx) is not None:
                start = _separator_before(ctx, splat_start)
                if start is not None:
                    yield keyword.value, "", (start, value_span[1]), None, None
                continue
            next_start = _next_argument_start(node, index, ctx)
            yield keyword.value, "", (splat_start, next_start if next_start is not None else value_span[1]), None, None


@dataclass(frozen=True, slots=True)
class KeepOperandSite(SiteFinder):
    """AOD (Laurent et al., ICST 2017; Petrović et al., TSE 2021): the binary operation becomes each of its
    operands in turn — the term a formula lost. Only an operation of two non-literal operands qualifies: dropping a
    literal operand repeats a constant shift, and keeping one is no fault."""

    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.BinOp):
            return
        if isinstance(node.left, ast.Constant) or isinstance(node.right, ast.Constant):
            return
        op = type(node.op).__name__
        for side, operand in (("lhs", node.left), ("rhs", node.right)):
            name = operator_name(_namespace(self.namespace), "binop", f"{op}->{side}")
            yield node, _atomic(ast.unparse(operand), operand), None, name, None


@dataclass(frozen=True, slots=True)
class DropBoolOperandSite(SiteFinder):
    """COR ``lhs``/``rhs`` (Just et al., STVR 2014) and PyTation's RemExpCond: one operand of an ``and``/``or``
    chain dropped, so the condition misses a clause. ``ast.unparse`` parenthesizes a lone operand whose precedence is
    lower than a boolean operand's (a conditional expression, a lambda, an assignment expression)."""

    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.BoolOp):
            return
        name = operator_name(_namespace(self.namespace), "boolop", f"{type(node.op).__name__}-drop")
        for index in range(len(node.values)):
            rest = [value for position, value in enumerate(node.values) if position != index]
            yield node, ast.unparse(ast.BoolOp(op=node.op, values=rest)), None, name, None


@dataclass(frozen=True, slots=True)
class StringLiteralSite(SiteFinder):
    """The string or bytes literal changed: a filled one wrapped in ``XX`` (as mutmut and poodle do), an empty
    one filled with ``XX``. The wrap keeps a filled string truthy and its text findable, so only a test that checks
    the exact text kills it."""

    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Constant):
            return
        value = node.value
        if isinstance(value, str):
            text, kind = repr(f"XX{value}XX" if value else "XX"), "str"
        elif isinstance(value, bytes):
            text, kind = repr(b"XX" + value + b"XX" if value else b"XX"), "bytes"
        else:
            return
        name = operator_name(_namespace(self.namespace), "const", f"{kind}:{'wrap' if value else 'fill'}")
        yield node, text, None, name, BcEdit(BcEditKind.CONST, "", "", text)


@dataclass(frozen=True, slots=True)
class SignDropSite(SiteFinder):
    """``-x`` becomes ``x`` (UOI, Laurent et al., ICST 2017): a lost sign, a negative index turned positive. Only the
    sign is spliced out: the operand of a negation is a power or another unary expression, which can stand wherever
    the negation stood, so the grouping never changes."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            sign = ctx.line_starts[node.lineno - 1] + node.col_offset
            yield node, "", (sign, sign + 1), None, None


@dataclass(frozen=True, slots=True)
class DropLastElementSite(SiteFinder):
    """RemElCont (PyTation): the last element of a list, tuple or set display, or the last entry of a dict display,
    removed with its separator: the element a fix appends ("Add Elements to Iterable", Kamienski et al., MSR 2021).
    One mutant per display of at least two elements in brackets of its own; a two-element tuple keeps the comma that
    makes it a tuple. The cut runs from the end of the element before the last to the closing bracket, short of
    a trailing comma and the whitespace before the bracket. A tuple without brackets is left alone: when its last
    element ends with a bracket (``a, f(b)``), the cut leaves that bracket unmatched and the edited text does not
    parse."""

    namespace: Library | str = Library.LANG

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        match node:
            case ast.List(elts=items) | ast.Tuple(elts=items) | ast.Set(elts=items) | ast.Dict(values=items):
                pass
            case _:
                return
        if len(items) < 2:
            return
        whole, previous = node_byte_span(node, ctx.line_starts), node_byte_span(items[-2], ctx.line_starts)
        if whole is None or previous is None:
            return
        data = ctx.source_bytes
        cut = whole[1] - 1
        if data[cut : cut + 1] not in (b"]", b"}", b")"):
            return
        while data[cut - 1 : cut] in (b" ", b"\t", b"\r", b"\n"):
            cut -= 1
        trailing_comma = data[cut - 1 : cut] == b","
        if trailing_comma:
            cut -= 1
        text = "," if isinstance(node, ast.Tuple) and len(items) == 2 and not trailing_comma else ""
        try:
            ast.parse(data[whole[0] : previous[1]] + text.encode() + data[cut : whole[1]])
        except SyntaxError:
            return
        yield node, text, (previous[1], cut), operator_name(_namespace(self.namespace), "container", "drop-last"), None


# --- Facts -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Replace:
    """A kit fact: every matched node is replaced with fixed text."""

    match: Matcher
    text: str
    operator: str

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is not None and isinstance(node, ast.expr | ast.stmt) and self.match.matches(node, ctx):
            ctx.emit(node, self.text, self.operator, fact=self)
        return ctx


@runtime_checkable
class ClassifiedSiteFinder(Protocol):
    """A site finder that names the semantic classes of the host node's sites (data, no parsing)."""

    def site_classes(self, node: ast.AST, ctx: EnumContext) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class Rewrite:
    """A kit fact: for every (target, text) the site finder yields over matched nodes, splice.

    ``transform`` is the piece doing the rewriting: a site finder that, for a matched node,
    yields ``(target, replacement text, splice span, operator name, bytecode edit)``. ``operator``
    is the fixed operator name when the fact produces exactly one; ``None`` (the default) when
    each site names its own operator in the yield. A site that names none when the fact names
    none is a contract error and raises. A ``ClassifiedSiteFinder`` also names the site's
    semantic classes."""

    match: Matcher
    transform: SiteFinder
    operator: str | None = None

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is not None and isinstance(node, ast.expr | ast.stmt) and self.match.matches(node, ctx):
            classes_hook = self.transform.site_classes if isinstance(self.transform, ClassifiedSiteFinder) else None
            for target, text, span, site_operator, bc in self.transform.find(node, ctx):
                operator = site_operator if site_operator is not None else self.operator
                if operator is None:
                    raise ValueError(f"{type(self).__name__}: a site yielded no operator and the fact names none")
                classes = classes_hook(node, ctx) if classes_hook is not None else ()
                ctx.emit(target, text, operator, fact=self, span=span, classes=classes, bc=bc)
        return ctx


# --- Region rewrites ----------------------------------------------------------


@runtime_checkable
class RegionRewriter(Protocol):
    """(node, replacement text) pairs rewriting the node's whole span, one mutant each."""

    def regions(self, node: ast.AST, ctx: EnumContext) -> Iterator[tuple[ast.expr | ast.stmt, str]]: ...


@dataclass(frozen=True, slots=True)
class RegionRewrite:
    """A whole-file phase: for each matched node, one mutant replacing the node's whole span.

    The transform owns the replacement text; ``line_indent`` and ``reindent`` keep multi-line
    replacements valid (the first line sits on the node's original indentation, which the span
    does not cover).
    """

    match: Matcher
    transform: RegionRewriter
    operator: str

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        ctx = context(path, source)
        for node in ast.walk(ast.parse(source)):
            if not self.match.matches(node, ctx):
                continue
            for target, text in self.transform.regions(node, ctx):
                span = node_byte_span(target, ctx.line_starts)
                if span is None:
                    continue
                ctx.emit(target, text, self.operator, fact=self, span=span)
        return ctx.mutants


@dataclass(frozen=True, slots=True)
class HoistFirstSuiteStatement:
    """(the block, the rewritten text): the first statement of the block's body moves out of it.

    The multi-line replacement keeps the original indentation: the hoisted statement sits on the
    block's own line (outside the span, which starts after the indent), and the rest of the block
    gets the indent back through ``reindent``."""

    def regions(self, node: ast.AST, ctx: EnumContext) -> Iterator[tuple[ast.expr | ast.stmt, str]]:
        if not isinstance(node, ast.With | ast.AsyncWith) or len(node.body) < 2:
            return
        modified = copy.deepcopy(node)
        modified.body = node.body[1:]
        hoisted = ast.unparse(node.body[0])
        block = reindent(ast.unparse(modified), line_indent(ctx, node), first=True)
        yield node, f"{hoisted}\n{block}"


def line_indent(ctx: EnumContext, node: ast.expr | ast.stmt) -> str:
    """The whitespace indentation of the node's line."""
    start = ctx.line_starts[node.lineno - 1]
    end = start
    while end < len(ctx.source_bytes) and ctx.source_bytes[end] in b" \t":
        end += 1
    return ctx.source_bytes[start:end].decode("utf-8")


def reindent(text: str, indent: str, *, first: bool) -> str:
    """Prefix every line (or all but the first, when ``first=False``) with ``indent``.

    ``first=False`` is the usual splice shape: the replaced node's own line keeps its original
    indentation (outside the span), while continuation lines need it.
    """
    lines = text.splitlines()
    if not lines:
        return ""
    if first:
        return "".join(f"{indent}{line}\n" for line in lines)
    head, tail = lines[0], lines[1:]
    return "".join([f"{head}\n", *(f"{indent}{line}\n" for line in tail)])
