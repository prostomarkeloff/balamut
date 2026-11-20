"""The ``src`` kind: kit compositions over the decision-logic mutation tables.

Every fact is a ``Rewrite``/``Replace`` composition; the swap and drop tables are plain data
passed into the kit's site finders. The kind's own concerns are the non-body exclusion (a
``body_site()`` matcher on every fact), the sites no test should be asked to pin (the arid nodes of
Petrović et al., TSE 2021: docstrings, logging, typing helpers, interpreter and platform probes) and the
default-argument phase (the only site whose covering line is the callers' body line, via the general
``emit(lineno=)`` parameter).

The operator set follows the studies of mutants against real faults: the sufficient relational replacements
(Kaminski et al., AST 2011; Just et al., STVR 2014), a condition forced to either constant and an ``and``/``or``
operand dropped (the non-redundant COR set), the operators most often coupled to real faults (conditional and
relational replacement and statement deletion; Just et al., FSE 2014), the extended arithmetic and constant
operators (Laurent et al., ICST 2017), the Python-specific removals of PyTation (Python conversions and
normalizers), and the commonest single-statement fixes of Python undone (Kamienski et al., MSR 2021; Richter and
Wehrheim, MSR 2022): the last element of a display dropped, a name or ``self`` method replaced with a similar one in
scope and a library method with its neighbour. Statement-level operators mutate function bodies only: a module or
class body runs once, at import.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from balamut.core.mutants import ApplyMechanism, BcEdit, Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import (
    AugAssignSwapSite,
    BinopSwapSite,
    BoolopSwapSite,
    CalleeDropSite,
    CalleeSwapSite,
    CompareSwapSite,
    DropBoolOperandSite,
    DropEachArgument,
    DropEachKeyword,
    DropEachMappingSplat,
    DropLastElementSite,
    FlipBoolSite,
    KeepOperandSite,
    Library,
    Matcher,
    MethodDropSite,
    MethodSwapSite,
    NumberShiftSite,
    Replace,
    Rewrite,
    SignDropSite,
    SiteFinder,
    StringLiteralSite,
    SwapAdjacentArgs,
    UnaryOperand,
    attr_name_span,
    const_of,
    context,
    expr_value,
    inside,
    node_is,
    operator_name,
    return_has_value,
    unary_op,
)

FAMILY = "src"


@dataclass(frozen=True, slots=True)
class BodySite(Matcher):
    """The node is inside a function body — not a default, annotation or decorator header
    (``ctx.non_body`` carries those)."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return id(node) not in ctx.non_body


def body_site() -> BodySite:
    return BodySite()


def in_function() -> Matcher:
    """The node lies in a function (or method) body: a statement-level mutant of a module or class body would
    change what runs once at import, which no in-memory patch reaches."""
    return inside(ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class SrcRewrite(Rewrite):
    """A src walk fact with its declared triage bucket (a fixed priority level, as data)."""

    triage: Bucket = Bucket.REVIEW

    def bucket(self, mutant: Mutant) -> Bucket:
        return self.triage


@dataclass(frozen=True, slots=True)
class SrcReplace(Replace):
    """A src replacement fact with its declared triage bucket (a fixed priority level, as data)."""

    triage: Bucket = Bucket.REVIEW

    def bucket(self, mutant: Mutant) -> Bucket:
        return self.triage


def _ancestors(node: ast.AST, ctx: EnumContext) -> Iterator[ast.AST]:
    """The node's parent, its parent's parent and so on up to the module."""
    current = ctx.parent_of(node)
    while current is not None:
        yield current
        current = ctx.parent_of(current)


def _reachable(func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: EnumContext) -> bool:
    """A top-level function or method: a nested function's ``__defaults__`` cannot be reached."""
    return not any(isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef) for ancestor in _ancestors(func, ctx))


@dataclass(frozen=True, slots=True)
class DefaultPosition(Matcher):
    """A literal positional default of a reachable (non-nested) function."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if not isinstance(node, ast.Constant):
            return False
        args = ctx.parent_of(node)
        if not isinstance(args, ast.arguments):
            return False
        if not any(default is node for default in args.defaults):
            return False
        func = ctx.parent_of(args)
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            return False  # a lambda's defaults are not reachable either
        return _reachable(func, ctx)


def default_position() -> DefaultPosition:
    return DefaultPosition()


def _zero_numeric(node: ast.expr) -> bool:
    """A numeric literal equal to 0 (bool excluded: True == 1 in Python)."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and node.value == 0
    )


@dataclass(frozen=True, slots=True)
class SwappableBinop(Matcher):
    """A BinOp/AugAssign whose operator is in the table and whose swap changes semantics
    (``x + 0`` and ``x - 0`` are the same value and type)."""

    swaps: tuple[tuple[type[ast.operator], type[ast.operator]], ...]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        match node:
            case ast.BinOp(op=op, right=right) | ast.AugAssign(op=op, value=right):
                if any(type(op) is from_ for from_, _to in self.swaps) and not _zero_numeric(right):
                    return True
            case _:
                pass
        return False


@dataclass(frozen=True, slots=True)
class OperandOperation(Matcher):
    """A binary operation whose operands are terms of one value (arithmetic, bitwise and set operators): not a
    matrix product, and not ``%`` formatting a string."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if not isinstance(node, ast.BinOp) or isinstance(node.op, ast.MatMult):
            return False
        formats = isinstance(node.left, ast.JoinedStr) or (
            isinstance(node.left, ast.Constant) and isinstance(node.left.value, str | bytes)
        )
        return not (isinstance(node.op, ast.Mod) and formats)


@dataclass(frozen=True, slots=True)
class ConditionTest(Matcher):
    """The test of an ``if``/``elif``, a conditional expression or a comprehension filter; with ``loops`` also of a
    ``while`` (forcing a loop test to true would only hang). A literal test is no condition to force."""

    loops: bool

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if isinstance(node, ast.Constant):
            return False
        match ctx.parent_of(node):
            case ast.If(test=test) | ast.IfExp(test=test):
                return test is node
            case ast.While(test=test):
                return self.loops and test is node
            case ast.comprehension(ifs=ifs):
                return any(condition is node for condition in ifs)
            case _:
                return False


# Interpreter, platform and type-checking probes: a branch on them selects the environment, not behavior a test pins.
_ENVIRONMENT_NAMES = frozenset({"TYPE_CHECKING", "__name__", "__debug__"})
_ENVIRONMENT_PATHS = frozenset(
    {
        "typing.TYPE_CHECKING",
        "sys.version_info",
        "sys.platform",
        "sys.implementation",
        "sys.byteorder",
        "sys.maxsize",
        "os.name",
        "platform.system",
        "platform.python_implementation",
    }
)


def _probes_environment(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        match sub:
            case ast.Name(id=name) if name in _ENVIRONMENT_NAMES:
                return True
            case ast.Attribute() if ast.unparse(sub) in _ENVIRONMENT_PATHS:
                return True
            case _:
                pass
    return False


@dataclass(frozen=True, slots=True)
class EnvironmentProbe(Matcher):
    """The expression probes the interpreter, the platform or the type checker, or lies in a comparison or boolean
    expression that does (``3`` in ``sys.version_info >= (3, 13)``)."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if isinstance(node, ast.expr) and _probes_environment(node):
            return True
        current = ctx.parent_of(node)
        while isinstance(current, ast.expr):
            if isinstance(current, ast.Compare | ast.BoolOp | ast.UnaryOp) and _probes_environment(current):
                return True
            current = ctx.parent_of(current)
        return False


# Calls whose string arguments no test should be asked to pin. Logging (Petrović et al.: 99 of 100 sampled
# suppressions correct) is matched on the method name and a receiver named like a logger (``log``, ``LOGGER``,
# ``app_logger``, ``logging``; not ``catalog`` or ``dialog``).
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})
_LOGGER_NAME = re.compile(r"(?:^|_)(?:log|logger|logging)$")
_ARID_CALLEES = frozenset(
    {
        "warn",
        "getLogger",
        "TypeVar",
        "TypeVarTuple",
        "ParamSpec",
        "NewType",
        "TypeAliasType",
        "cast",
    }
)
_TYPE_SUBSCRIPTS = frozenset({"Literal", "Annotated"})
_DECLARATION_NAMES = frozenset({"__all__", "__slots__"})


def _callee_name(func: ast.expr) -> str | None:
    match func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return None


def _arid_call(call: ast.Call) -> bool:
    name = _callee_name(call.func)
    if name in _ARID_CALLEES:
        return True
    match call.func:
        case ast.Attribute(value=receiver) if name in _LOG_METHODS:
            return _LOGGER_NAME.search((_callee_name(receiver) or "").lower()) is not None
        case _:
            return False


def _arid_expression(node: ast.AST) -> bool:
    """A call or a type subscript whose string arguments no test should be asked to pin."""
    match node:
        case ast.Call():
            return _arid_call(node)
        case ast.Subscript(value=value):
            return _callee_name(value) in _TYPE_SUBSCRIPTS
        case _:
            return False


def _declares(statement: ast.AST) -> bool:
    match statement:
        case ast.Assign(targets=targets):
            return any(isinstance(target, ast.Name) and target.id in _DECLARATION_NAMES for target in targets)
        case ast.AugAssign(target=ast.Name(id=name)) | ast.AnnAssign(target=ast.Name(id=name)):
            return name in _DECLARATION_NAMES
        case _:
            return False


@dataclass(frozen=True, slots=True)
class AridString(Matcher):
    """A string literal no test should be asked to pin: a docstring or other bare string statement, a literal part
    of an f-string, an assertion message, an argument of logging, ``warnings.warn`` or a typing helper, a
    ``Literal[...]``/``Annotated[...]`` member, or an ``__all__``/``__slots__`` entry. (A side of an environment
    probe, ``sys.platform == "win32"``, is ``EnvironmentProbe``'s.)"""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        parent = ctx.parent_of(node)
        if isinstance(parent, ast.Expr) or type(parent).__name__ in {"JoinedStr", "TemplateStr"}:
            return True
        if isinstance(parent, ast.Assert):
            return parent.msg is node
        ancestors = list(_ancestors(node, ctx))
        statement = next(index for index, ancestor in enumerate(ancestors) if isinstance(ancestor, ast.stmt))
        return any(_arid_expression(ancestor) for ancestor in ancestors[:statement]) or _declares(ancestors[statement])


@dataclass(frozen=True, slots=True)
class AssignedValue(Matcher):
    """The value of a plain assignment to names, attributes or subscripts (an unpacking target would fail on
    ``None`` for every input), unless it is a literal or an empty display: those have their own operators, or fail
    on first use."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        match ctx.parent_of(node):
            case ast.Assign(targets=targets, value=value) if value is node:
                unpacks = any(isinstance(target, ast.Tuple | ast.List) for target in targets)
            case ast.AnnAssign(value=value) if value is node:
                unpacks = False  # an annotated target is a name, an attribute or a subscript
            case _:
                return False
        literal = isinstance(node, ast.Constant) or (
            isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)
        )
        empty = (isinstance(node, ast.List | ast.Tuple) and not node.elts) or (
            isinstance(node, ast.Dict) and not node.keys
        )
        return not (unpacks or literal or empty)


_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})


@dataclass(frozen=True, slots=True)
class HandlerType(Matcher):
    """The exception type of an ``except`` clause of a plain ``try`` (an ``except*`` cannot match nothing); with
    ``specific`` only one that does not already catch ``Exception`` or ``BaseException``."""

    specific: bool

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        handler = ctx.parent_of(node)
        if not isinstance(handler, ast.ExceptHandler) or handler.type is not node:
            return False
        if not isinstance(ctx.parent_of(handler), ast.Try):
            return False
        caught = node.elts if isinstance(node, ast.Tuple) else [node]
        return bool(caught) and not (self.specific and any(ast.unparse(kind) in _BROAD_EXCEPTIONS for kind in caught))


@dataclass(frozen=True, slots=True)
class ValueUsed(Matcher):
    """The expression's value is used: it is not a bare expression statement."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return not isinstance(ctx.parent_of(node), ast.Expr)


@dataclass(frozen=True, slots=True)
class DataDisplay(Matcher):
    """A list, tuple, set or dict display read as data: not an assignment or loop target, not a returned or yielded
    tuple of values or one unpacked into several targets (dropping one of those changes the arity every caller
    unpacks), and not an argument of logging, ``warnings.warn`` or a typing helper."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if isinstance(node, ast.List | ast.Tuple) and not isinstance(node.ctx, ast.Load):
            return False
        if not isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict):
            return False
        if isinstance(node, ast.Tuple):
            match ctx.parent_of(node):
                case ast.Return() | ast.Yield() | ast.YieldFrom():
                    return False
                case ast.Assign(targets=targets) if any(isinstance(t, ast.Tuple | ast.List) for t in targets):
                    return False
                case _:
                    pass
        return not any(_arid_expression(ancestor) for ancestor in _ancestors(node, ctx))


@dataclass(frozen=True, slots=True)
class ScopeFacts:
    """What the name operator needs from the whole module, computed once per file: per eligible site (a variable
    load or a ``self`` method called, by node id) the similar name that replaces it."""

    similar: dict[int, str]


_SCOPE_KEY = "src:scope"
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTED_SCOPES = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _words(name: str) -> tuple[str, ...]:
    """The words of a snake_case or camelCase name, lowercased, digits apart."""
    return tuple(word.lower() for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name))


def _alike(name: str, other: str) -> bool:
    """Whether two names are the same words but one (``start_line``/``end_line``, ``val1``/``val2``), neither of them
    private or special. One-word names one edit apart are not alike: in one scope they are mostly a collection and
    its element (``items``/``item``), and on the PyMutBench corpus their swaps revealed no fault the one-word swaps
    miss."""
    words, others = _words(name), _words(other)
    return (
        not name.startswith("__")
        and not other.startswith("__")
        and len(words) == len(others) >= 2
        and sum(x != y for x, y in zip(words, others, strict=True)) == 1
    )


def _closest(name: str, candidates: Iterable[str]) -> str | None:
    """The first alike candidate in sorted order (one replacement per site, the same on every run)."""
    return min((other for other in candidates if _alike(name, other)), default=None)


def _own_nodes(scope: ast.AST, *, comprehensions: bool) -> Iterator[ast.AST]:
    """The nodes of a function's own scope: nested functions, lambdas and classes left out, comprehensions (their
    own scope for the names they bind) only with ``comprehensions``."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES) or (not comprehensions and isinstance(node, _COMPREHENSIONS)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _functions_in(scope: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions a scope defines, its classes' methods among them, but none defined inside another function."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, _FUNCTIONS):
            yield node
        else:
            stack.extend(ast.iter_child_nodes(node))


def _bound(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a function binds: its parameters and every name its own scope stores to."""
    args = func.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if arg}
    for node in _own_nodes(func, comprehensions=False):
        match node:
            case ast.Name(id=name, ctx=ast.Store()):
                names.add(name)
            case ast.ExceptHandler(name=str(name)):
                names.add(name)
            case _:
                pass
    return names


def _similar_names(tree: ast.Module, ctx: EnumContext) -> dict[int, str]:
    """Per variable load in a function (a name the function or an enclosing one binds), the most similar other such
    name; per method called on the first parameter of a method, the most similar other method of its class (one the
    class defines or one called on that parameter: a data attribute called fails at once)."""
    found: dict[int, str] = {}

    def visit_function(func: ast.FunctionDef | ast.AsyncFunctionDef, outer: frozenset[str]) -> None:
        visible = outer | _bound(func)
        for node in _own_nodes(func, comprehensions=True):
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in visible):
                continue
            parent = ctx.parent_of(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            replacement = _closest(node.id, visible)
            if replacement is not None:
                found[id(node)] = replacement
        for nested in _functions_in(func):
            visit_function(nested, visible)

    def visit_class(cls: ast.ClassDef) -> None:
        methods = [m for m in cls.body if isinstance(m, _FUNCTIONS)]
        names = {m.name for m in methods}
        calls: list[ast.Attribute] = []
        for method in methods:
            positional = [*method.args.posonlyargs, *method.args.args]
            if not positional:
                continue
            receiver = positional[0].arg
            for node in ast.walk(method):
                match node:
                    case ast.Call(func=ast.Attribute(value=ast.Name(id=name)) as called) if name == receiver:
                        names.add(called.attr)
                        calls.append(called)
                    case _:
                        pass
        for called in calls:
            replacement = _closest(called.attr, names)
            if replacement is not None:
                found[id(called)] = replacement

    for func in _functions_in(tree):
        visit_function(func, frozenset())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            visit_class(node)
    return found


def _scope_facts(ctx: EnumContext) -> ScopeFacts:
    facts = ctx.prepared(_SCOPE_KEY)
    assert isinstance(facts, ScopeFacts), "the scope facts are prepared before the walk"
    return facts


@dataclass(frozen=True, slots=True)
class ScopedRewrite(SrcRewrite):
    """A src walk fact that reads the module's scope facts, prepared once per file before the walk."""

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext:
        ctx.remember(_SCOPE_KEY, ScopeFacts(_similar_names(tree, ctx)))
        return ctx


@dataclass(frozen=True, slots=True)
class HasSimilarName(Matcher):
    """A variable load or a ``self`` method called that has a similar name in scope (the prepared scope facts)."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return id(node) in _scope_facts(ctx).similar


@dataclass(frozen=True, slots=True)
class SimilarNameSite(SiteFinder):
    """The commonest single-statement bugs of Python, "Change Identifier Used" and "Wrong Function/Method Name"
    (Kamienski et al., MSR 2021; Richter and Wehrheim, MSR 2022): a name replaced with the most similar other name in
    scope, the tailored identifier operator of Allamanis et al. (2016) restricted to names that differ in one word. A
    method call on ``self`` gets another method of the class (a similar method called, Just et al., FSE 2014); an
    attribute read keeps its name, as on the PyMutBench corpus its swaps revealed no fault the other operators miss.
    Only a site ``HasSimilarName`` matched has a replacement."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        match node:
            case ast.Name():
                replacement = _scope_facts(ctx).similar[id(node)]
                yield node, replacement, None, operator_name(Library.LANG, "name", "similar"), None
            case ast.Attribute():
                span = attr_name_span(node, ctx)
                if span is not None:
                    replacement = _scope_facts(ctx).similar[id(node)]
                    yield node, replacement, span, operator_name(Library.LANG, "method", "similar"), None
            case _:
                pass


_COMPARE_SWAP: tuple[tuple[type[ast.cmpop], type[ast.cmpop]], ...] = (
    (ast.Lt, ast.LtE),
    (ast.LtE, ast.Lt),
    (ast.Gt, ast.GtE),
    (ast.GtE, ast.Gt),
    (ast.Eq, ast.NotEq),
    (ast.NotEq, ast.Eq),
    (ast.Is, ast.IsNot),
    (ast.IsNot, ast.Is),
    (ast.In, ast.NotIn),
    (ast.NotIn, ast.In),
)
# With ``_COMPARE_SWAP``, the sufficient replacements of each relational operator (Kaminski et al., AST 2011; Just
# et al., STVR 2014): the three that together subsume every other replacement over the intervals a < b, a == b and
# a > b. The constant replacement of a whole condition is the ``lang:condition`` facts' job. Python operands need
# not be ordered, so ``==``/``!=`` keep their negation as well: an ordering replacement that raises cannot stand in
# for it.
_COMPARE_SUFFICIENT: tuple[tuple[type[ast.cmpop], type[ast.cmpop]], ...] = (
    (ast.Lt, ast.NotEq),
    (ast.LtE, ast.Eq),
    (ast.Gt, ast.NotEq),
    (ast.GtE, ast.Eq),
    (ast.Eq, ast.LtE),
    (ast.Eq, ast.GtE),
    (ast.NotEq, ast.Lt),
    (ast.NotEq, ast.Gt),
)
_BINOP_SWAP: tuple[tuple[type[ast.operator], type[ast.operator]], ...] = (
    (ast.Add, ast.Sub),
    (ast.Sub, ast.Add),
    (ast.Mult, ast.Div),
    (ast.Div, ast.Mult),
    (ast.Mod, ast.Mult),
    (ast.Pow, ast.Mult),
    (ast.FloorDiv, ast.Div),
    # True and floor division: the missing or wrong integer conversion (Just et al., FSE 2014).
    (ast.Div, ast.FloorDiv),
    # Bitwise and shift operators: flags, masks and permissions.
    (ast.BitAnd, ast.BitOr),
    (ast.BitOr, ast.BitAnd),
    (ast.BitXor, ast.BitAnd),
    (ast.LShift, ast.RShift),
    (ast.RShift, ast.LShift),
)
_BOOLOP_SWAP: tuple[tuple[type[ast.boolop], type[ast.boolop]], ...] = (
    (ast.And, ast.Or),
    (ast.Or, ast.And),
)
# Builtin callee swaps: direction of clamps/quantifiers. A new swap is one table row.
_BUILTIN_SWAP: tuple[tuple[str, str], ...] = (("min", "max"), ("max", "min"), ("all", "any"), ("any", "all"))
# Method swaps: only known directional pairs; the receiver type is not checked.
_METHOD_SWAP: tuple[tuple[str, str], ...] = (
    ("startswith", "endswith"),
    ("endswith", "startswith"),
    ("lstrip", "rstrip"),
    ("rstrip", "lstrip"),
    ("find", "rfind"),
    ("rfind", "find"),
    # `index -> rindex` is excluded: lists and tuples have `.index()` but no `.rindex()`, so the mutant would
    # only "die" of AttributeError. `rindex -> index` is safe: `.rindex()` exists only where `.index()` does.
    ("rindex", "index"),
)
# The similar library method called (Just et al., FSE 2014; "Wrong Function/Method Name", Kamienski et al., MSR
# 2021): the same receiver and arguments, the neighbouring operation of a list, a mapping, a string, a regular
# expression, a set, ``math`` or ``os.path``. ``append -> extend`` is left out: the commonest of these calls, it gave
# most of the table's mutants and, on the PyMutBench corpus, revealed no fault the other operators miss.
_SIMILAR_METHODS: tuple[tuple[str, str], ...] = (
    ("extend", "append"),
    ("keys", "values"),
    ("values", "keys"),
    ("lower", "upper"),
    ("upper", "lower"),
    ("match", "search"),
    ("search", "match"),
    ("fullmatch", "match"),
    ("partition", "rpartition"),
    ("rpartition", "partition"),
    ("ljust", "rjust"),
    ("rjust", "ljust"),
    ("issubset", "issuperset"),
    ("issuperset", "issubset"),
    ("floor", "ceil"),
    ("ceil", "floor"),
    ("basename", "dirname"),
    ("dirname", "basename"),
)
# Builtin drops: the call returns its operand rounded, converted, ordered or deduplicated, so the operand stands in
# for it (PyTation's RemConvFunc; ``abs`` is the ABS operator of Laurent et al.).
_BUILTIN_DROP = frozenset({"round", "abs", "int", "float", "str", "sorted", "reversed", "set", "frozenset"})
_METHOD_DROP = frozenset({"quantize"})
# String normalizers whose result is used: the receiver stands in for it (an omitted chained call, Just et al.,
# FSE 2014; PyTation's RemMetCall).
_STR_NORMALIZERS = frozenset({"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "title", "capitalize"})


_WALK_FACTS: OperatorKind = (
    SrcRewrite(
        match=body_site() & node_is(ast.Compare),
        transform=CompareSwapSite(_COMPARE_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.BinOp) & SwappableBinop(_BINOP_SWAP),
        transform=BinopSwapSite(_BINOP_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.BoolOp),
        transform=BoolopSwapSite(_BOOLOP_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.AugAssign) & SwappableBinop(_BINOP_SWAP),
        transform=AugAssignSwapSite(_BINOP_SWAP),
    ),
    SrcRewrite(
        match=body_site() & unary_op(ast.Not),
        transform=UnaryOperand(),
        operator=operator_name(Library.LANG, "unary", "not-removal"),
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Break),
        text="continue",
        operator=operator_name(Library.LANG, "control", "break->continue"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Continue),
        text="break",
        operator=operator_name(Library.LANG, "control", "continue->break"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=CalleeSwapSite(_BUILTIN_SWAP, namespace=operator_name(Library.STD, "builtins")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=MethodSwapSite(_METHOD_SWAP, namespace=operator_name(Library.STD, "str")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=MethodSwapSite(_SIMILAR_METHODS, namespace=operator_name(Library.STD, "method")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=CalleeDropSite(_BUILTIN_DROP, namespace=operator_name(Library.STD, "builtins")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=MethodDropSite(_METHOD_DROP, namespace=operator_name(Library.STD, "decimal")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=SwapAdjacentArgs(),
        operator=operator_name(Library.LANG, "call", "arg-swap"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=DropEachArgument(),
        operator=operator_name(Library.LANG, "call", "arg-drop"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=DropEachKeyword(),
        operator=operator_name(Library.LANG, "call", "kwarg-drop"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(match=body_site() & (const_of(int) | const_of(float)), transform=NumberShiftSite(1)),
    SrcRewrite(match=body_site() & const_of(bool), transform=FlipBoolSite(), triage=Bucket.HIGH),
    SrcReplace(
        match=body_site() & return_has_value(),
        text="return None",
        operator=operator_name(Library.LANG, "return", "None"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Expr) & expr_value(node_is(ast.Call)),
        text="pass",
        operator=operator_name(Library.LANG, "delete", "call"),
        triage=Bucket.HIGH,
    ),
    # Comparisons: the rest of the sufficient relational replacements.
    SrcRewrite(
        match=body_site() & node_is(ast.Compare) & ~EnvironmentProbe(),
        transform=CompareSwapSite(_COMPARE_SUFFICIENT),
        triage=Bucket.HIGH,
    ),
    # Constants: the other side of an off-by-one (CRCR, Laurent et al.), a sign dropped, a string literal changed.
    SrcRewrite(
        match=body_site() & (const_of(int) | const_of(float)) & ~EnvironmentProbe(),
        transform=NumberShiftSite(-1),
    ),
    SrcRewrite(
        match=body_site() & unary_op(ast.USub) & ~EnvironmentProbe(),
        transform=SignDropSite(),
        operator=operator_name(Library.LANG, "unary", "neg-removal"),
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Constant) & ~AridString() & ~EnvironmentProbe(),
        transform=StringLiteralSite(),
    ),
    # Conditions: forced to either constant (``while`` only to false), and an ``and``/``or`` operand dropped.
    SrcReplace(
        match=body_site() & in_function() & ConditionTest(loops=False) & ~EnvironmentProbe(),
        text="True",
        operator=operator_name(Library.LANG, "condition", "True"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & in_function() & ConditionTest(loops=True) & ~EnvironmentProbe(),
        text="False",
        operator=operator_name(Library.LANG, "condition", "False"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.BoolOp) & ~EnvironmentProbe(),
        transform=DropBoolOperandSite(),
        triage=Bucket.HIGH,
    ),
    # Arithmetic: a term of the formula lost.
    SrcRewrite(match=body_site() & OperandOperation(), transform=KeepOperandSite(), triage=Bucket.HIGH),
    # State and control: an assigned value lost, a handler that catches nothing or everything.
    SrcReplace(
        match=body_site() & in_function() & AssignedValue(),
        text="None",
        operator=operator_name(Library.LANG, "assign", "None"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & in_function() & HandlerType(specific=False),
        text="()",
        operator=operator_name(Library.LANG, "except", "never"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & in_function() & HandlerType(specific=True),
        text="Exception",
        operator=operator_name(Library.LANG, "except", "Exception"),
        triage=Bucket.HIGH,
    ),
    # Calls: forwarded options lost, a normalizing method call omitted.
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=DropEachMappingSplat(),
        operator=operator_name(Library.LANG, "call", "kwargs-drop"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call) & ValueUsed(),
        transform=MethodDropSite(_STR_NORMALIZERS, namespace=operator_name(Library.STD, "str")),
        triage=Bucket.HIGH,
    ),
    # What a fix adds most often to one statement (Kamienski et al., MSR 2021): the element a collection lacked, and
    # the right name in place of a similar one.
    SrcRewrite(match=body_site() & DataDisplay(), transform=DropLastElementSite(), triage=Bucket.HIGH),
    ScopedRewrite(
        match=body_site() & node_is(ast.Name, ast.Attribute) & HasSimilarName(),
        transform=SimilarNameSite(),
        triage=Bucket.HIGH,
    ),
)


# The literal changes of a default value: both off-by-ones, a negated boolean, a changed string.
_DEFAULT_FINDERS: tuple[SiteFinder, ...] = (
    NumberShiftSite(1),
    NumberShiftSite(-1),
    FlipBoolSite(),
    StringLiteralSite(),
)


@dataclass(frozen=True, slots=True)
class DefaultsFact:
    """The src kind's trailing phase: literal defaults through ``__defaults__`` (positional) and
    ``__kwdefaults__`` (keyword-only).

    The covering line is the first executable body line (covering tests call the function, not the
    ``def`` line), which is exactly the general ``emit(lineno=)`` parameter.
    """

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        tree = ast.parse(source)
        ctx = context(path, source, tree=tree)
        matcher = default_position()
        for func in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)):
            body_stmts = func.body[1:] if _is_docstring(func.body[0]) else func.body
            body_line = (body_stmts[0] if body_stmts else func.body[0]).lineno
            for index, default in enumerate(func.args.defaults):
                if matcher.matches(default, ctx):
                    self._emit(ctx, default, body_line, default_index=index)
            if _reachable(func, ctx):
                # ``kw_defaults`` runs parallel to ``kwonlyargs``, with None for an argument without a default.
                for index, argument in enumerate(func.args.kwonlyargs):
                    match func.args.kw_defaults[index]:
                        case ast.Constant() as default:
                            self._emit(ctx, default, body_line, default_name=argument.arg)
                        case _:
                            pass
        return ctx.mutants

    def _emit(
        self,
        ctx: EnumContext,
        default: ast.expr,
        body_line: int,
        *,
        default_index: int | None = None,
        default_name: str | None = None,
    ) -> None:
        for finder in _DEFAULT_FINDERS:
            for target, text, _span, op, _bc in finder.find(default, ctx):
                if op is None:
                    continue
                ctx.emit(
                    target,
                    text,
                    op,
                    fact=self,
                    lineno=body_line,
                    site_lineno=default.lineno,
                    default_index=default_index,
                    default_name=default_name,
                )

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.DEFAULTS

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a default value is part of the call contract


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


# The kind as campaigns run it: the walk facts, then the default-argument phase.
src_kind: OperatorKind = (*_WALK_FACTS, DefaultsFact())


def enumerate_mutants(path: str, source: str) -> list[Mutant]:
    """All syntactic (``src``) mutants of a module: the kit facts plus the defaults phase."""
    return enumerate_facts(path, source, (src_kind,), family=FAMILY)


def enumerate_defaults(path: str, source: str) -> list[Mutant]:
    """Mutants of literal default values, applied through ``__defaults__``/``__kwdefaults__``."""
    return enumerate_facts(path, source, ((DefaultsFact(),),), family=FAMILY)
