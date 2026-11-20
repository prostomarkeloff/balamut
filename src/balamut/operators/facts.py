"""The capability layer of the operator API: protocols, contexts and the enumeration fold.

A *fact* is any frozen value that speaks the operator protocols — there is no base class, no
registry and no closed set of kinds. The engine's folds dispatch on protocols structurally
(``Sites`` for the shared AST walk, ``Enumerates`` for whole-file phases); the report label of a
mutant is the kind's name, stamped by the fold; behavior flags are marker attributes (``Fuzzable``,
``SelfCovered``, ``LaneIsolated``). A new kind, a new family and a new behavior are each an
addition, never an edit to the core.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from capability import Capability, Step, by_protocol, fold

from balamut.core.mutants import ApplyMechanism, BcEdit, Mutant, non_body_node_ids
from balamut.core.source_spans import line_byte_starts, node_byte_span

type OperatorKind = tuple[Capability, ...]
"""A named mutant kind: the facts that enumerate it, in emission order. Any frozen value
implementing at least one producing protocol is a fact."""

type FactMemo = dict[str, Capability]  # per-kind prepared data, keyed by the owning fact's name


@runtime_checkable
class Sites(Protocol):
    """A fact contributes mutants while the engine walks the AST."""

    def sites(self, ctx: EnumContext) -> EnumContext: ...


@runtime_checkable
class Prepare(Protocol):
    """A walk fact runs one tree-wide analysis per file, before the walk.

    The result goes into ``ctx.remember(key, value)``, read by the same fact's ``sites`` through
    ``ctx.prepared(key)`` — this is what lets tree-wide state (discarded awaits, collectable-test
    ids) feed walk facts without a whole-file phase.
    """

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext: ...


@runtime_checkable
class Enumerates(Protocol):
    """A fact enumerates a whole file by itself: it needs whole-tree state or its own matcher."""

    def enumerate(self, path: str, source: str) -> list[Mutant]: ...


@runtime_checkable
class Mechanized(Protocol):
    """The apply mechanism the fact's mutants are designed for; CODE when absent."""

    def mechanism(self) -> ApplyMechanism: ...


def mutant_mechanism(mutant: Mutant) -> ApplyMechanism:
    """The apply mechanism of a mutant: its fact's declaration, CODE when absent."""
    fact = mutant.fact
    return fact.mechanism() if isinstance(fact, Mechanized) else ApplyMechanism.CODE


def effective_mechanism(mutant: Mutant, *, code_reachable: bool) -> ApplyMechanism:
    """The mechanism of this particular mutant: a CODE mutant outside any function body is COLD."""
    intrinsic = mutant_mechanism(mutant)
    if intrinsic is ApplyMechanism.CODE and not code_reachable:
        return ApplyMechanism.COLD
    return intrinsic


@runtime_checkable
class Fuzzable(Protocol):
    """A fact declares ``fuzz: bool = True``: schedule-fuzz yield injection applies to its mutants."""

    fuzz: bool


@runtime_checkable
class SelfCovered(Protocol):
    """A fact declares ``self_covered: bool = True``: the covering test of its mutants is the mutant's
    own function (coverage does not cover it), so the identity path selects by qualname."""

    self_covered: bool


@runtime_checkable
class LaneIsolated(Protocol):
    """A fact declares ``isolated_lane: bool = True``: its mutants run in the isolated single-worker
    lane, one worker per daemon."""

    isolated_lane: bool


type ParentMap = dict[int, ast.AST]  # id(node) -> parent, for context-aware matchers


@dataclass(slots=True)
class EnumContext:
    """The context threaded through the facts of one module during the walk.

    The engine sets ``node`` per node before folding the facts of a kind. Facts mutate the
    context and must return the very context they were given (asserted by the step). ``emit``
    computes the byte-exact span, the original text and the site lines, mirroring exactly the
    enumerators the facts are ported from.
    """

    path: str
    source_bytes: bytes
    line_starts: list[int]
    non_body: frozenset[int]
    node: ast.AST | None = None
    parents: ParentMap | None = None
    memo: FactMemo | None = None
    mutants: list[Mutant] = field(default_factory=list[Mutant])

    def parent_of(self, node: ast.AST) -> ast.AST | None:
        """The parent of ``node`` (None at the module root or without the parent map)."""
        if self.parents is None:
            return None
        return self.parents.get(id(node))

    def prepared(self, key: str) -> Capability | None:
        """Data a Prepare fact stored under ``key`` (None without it)."""
        if self.memo is None:
            return None
        return self.memo.get(key)

    def remember(self, key: str, value: Capability) -> None:
        """Store prepared data under ``key`` for the walk facts of this kind."""
        if self.memo is None:
            self.memo = {}
        self.memo[key] = value

    def suite_of(self, node: ast.AST) -> list[ast.stmt] | None:
        """The statement list the node is a direct member of, or None."""
        parent = self.parent_of(node)
        if parent is None:
            return None
        for suite in _suite_candidates(parent):
            for statement in suite:
                if statement is node:
                    return suite
        return None

    def suite_index(self, node: ast.AST) -> int | None:
        """The node's position in its statement suite, or None outside one."""
        suite = self.suite_of(node)
        if suite is None:
            return None
        for index, statement in enumerate(suite):
            if statement is node:
                return index
        return None

    def emit(
        self,
        target: ast.expr | ast.stmt,
        mutated: str,
        operator: str,
        *,
        fact: Capability | None = None,
        span: tuple[int, int] | None = None,
        lineno: int | None = None,
        site_lineno: int | None = None,
        classes: tuple[str, ...] = (),
        default_index: int | None = None,
        default_name: str | None = None,
        bc: BcEdit | None = None,
    ) -> None:
        """Append the mutant replacing ``target`` with ``mutated``; a no-op replacement is skipped.

        ``fact`` is the emitting fact itself, recorded on the mutant (its family and its behavior
        flags travel with it). ``span`` overrides the splice span when the edit is wider than the
        target (a removed argument with its separator, a rewritten pair tuple); line and site-line
        fields always come from ``target``. ``classes`` are the semantic classes of the site (the
        fact owns their meaning)."""
        resolved = span if span is not None else node_byte_span(target, self.line_starts)
        if resolved is None:
            return
        start, end = resolved
        original = self.source_bytes[start:end].decode("utf-8")
        if mutated == original:
            return
        end_line = target.end_lineno or target.lineno
        self.mutants.append(
            Mutant(
                self.path,
                lineno if lineno is not None else target.lineno,
                start,
                end,
                original,
                mutated,
                operator,
                site_lineno=site_lineno if site_lineno is not None else -1,
                site_end_lineno=end_line,
                fact=fact,
                family="",
                semantic=classes,
                default_index=default_index,
                default_name=default_name,
                bc=bc,
            )
        )


def _suite_candidates(parent: ast.AST) -> list[list[ast.stmt]]:
    """The statement lists of a parent AST node (empty for non-suite parents)."""
    match parent:
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef() | ast.With() | ast.AsyncWith():
            return [parent.body]
        case ast.If() | ast.While() | ast.For() | ast.AsyncFor():
            return [parent.body, parent.orelse]
        case ast.Try() | ast.TryStar():
            return [parent.body, parent.orelse, parent.finalbody, *(handler.body for handler in parent.handlers)]
        case ast.ExceptHandler():
            return [parent.body]
        case ast.Match():
            return [case.body for case in parent.cases]
        case _:
            return []


def _prepare_apply(fact: Prepare, ctx: EnumContext, tree: ast.Module) -> EnumContext:
    result = fact.prepare(ctx, tree)
    assert result is ctx, "a fact must return the context it was given"
    return result


def prepare_step(tree: ast.Module) -> Step[Capability, EnumContext]:
    """The fold step of the per-file preparation pass (built per file: it closes over the tree)."""
    return by_protocol(Prepare, lambda fact, ctx: _prepare_apply(fact, ctx, tree))


def _sites_apply(fact: Sites, ctx: EnumContext) -> EnumContext:
    result = fact.sites(ctx)
    assert result is ctx, "a fact must return the context it was given"
    return result


sites_step = by_protocol(Sites, _sites_apply)
"""The step of the enumeration fold: apply a fact's ``sites``, skip facts without it."""


def enumerate_facts(path: str, source: str, kinds: Sequence[OperatorKind], *, family: str = "") -> list[Mutant]:
    """One parse and one walk for the Sites facts of all ``kinds``, with per-kind phase facts.

    A kind is a tuple of facts. The Sites facts of every kind fold over one shared walk, each kind
    keeping its own context, so the emission order stays per-kind and byte-identical to the
    enumerators this replaced. A fact implementing ``Enumerates`` enumerates a whole file by
    itself (whole-tree state or its own matcher): such a fact before the kind's first Sites fact
    runs before its walk mutants, one after runs after them — the kind's block keeps its exact
    composition order.

    ``family`` is the kind's report label, stamped on every mutant the fold produces."""
    tree = ast.parse(source)
    source_bytes = source.encode("utf-8")
    line_starts = line_byte_starts(source)
    non_body = non_body_node_ids(tree)
    parents: ParentMap = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    records: list[tuple[list[Enumerates], EnumContext | None, OperatorKind, list[Enumerates]]] = []
    for kind in kinds:
        leading: list[Enumerates] = []
        walk: list[Capability] = []
        trailing: list[Enumerates] = []
        for fact in kind:
            if isinstance(fact, Enumerates):
                (leading if not walk else trailing).append(fact)
            else:
                walk.append(fact)
        context = EnumContext(path, source_bytes, line_starts, non_body, parents=parents) if walk else None
        records.append((leading, context, tuple(walk), trailing))
    prepared_step = prepare_step(tree)
    for _leading, context, kind_walk, _trailing in records:
        if context is not None:
            fold(kind_walk, context, prepared_step)
    for node in ast.walk(tree):
        for _leading, context, kind_walk, _trailing in records:
            if context is not None:
                context.node = node
                fold(kind_walk, context, sites_step)
    mutants: list[Mutant] = []
    for leading, context, _kind_walk, trailing in records:
        for fact in leading:
            mutants.extend(fact.enumerate(path, source))
        if context is not None:
            mutants.extend(context.mutants)
        for fact in trailing:
            mutants.extend(fact.enumerate(path, source))
    if family:
        mutants = [mutant if mutant.family else replace(mutant, family=family) for mutant in mutants]
    return mutants
