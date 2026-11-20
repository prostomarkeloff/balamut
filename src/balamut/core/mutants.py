"""The mutant record and its byte-precise splice.

The operator kinds live in ``balamut.operators`` as facts on the capability fold; the record
carries the fact itself plus its free-form ``family`` label (set at emission) for the store
and the reports. Spans are measured in bytes because CPython's ``col_offset`` is a UTF-8 byte
offset.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capability import Capability


class ApplyMechanism(Enum):
    """How a mutant is physically applied to the live process.

    The hot path applies only in-memory mechanisms; ``COLD`` marks sites that would need a module re-import."""

    CODE = "code"  # replace func.__code__: the node is in a function body
    DEFAULTS = "defaults"  # replace func.__defaults__: a literal default argument
    COLD = "cold"  # needs a re-import: module or class level, decorators


class BcEditKind(Enum):
    """The kind of a size-preserving bytecode edit a fact declares structurally."""

    CONST = "const"  # a literal: the new value replaces the loaded constant
    BINOP = "binop"  # a binary operator: the instruction arg becomes the target's
    COMPARE = "compare"  # a comparison: the instruction arg becomes the target's


@dataclass(frozen=True, slots=True)
class BcEdit:
    """A bytecode-editable edit, declared by the producing finder (never parsed from a string)."""

    kind: BcEditKind
    source: str  # binop: the operator name swapped from (the precedence gate needs it)
    target: str  # binop/compare: the operator name swapped to
    mutated: str  # const: the literal text of the new value


@dataclass(frozen=True, slots=True)
class Mutant:
    path: str
    lineno: int
    start_byte: int
    end_byte: int
    original: str
    mutated: str
    operator: str
    # Line of the mutation itself, for the diff scope. Equals ``lineno`` except for default-argument mutants:
    # their ``lineno`` is the first body line (covering tests are selected by callers) while the edit sits on
    # the ``def`` line. Without it a diff touching only a default would fall outside the changed-line scope.
    site_lineno: int = -1
    # Last line of the mutated span: the diff scope checks the whole ``[site_lineno, site_end_lineno]`` range.
    site_end_lineno: int = -1
    # The fact that produced the mutant: any frozen value speaking the operator protocols (its family, its
    # semantic classes and its behavior flags travel with it). Never part of the cache key: the operator
    # string stays the serialized identity.
    fact: Capability | None = None
    # The report/store label of the family, set at emission from the fact; a free-form string, open world.
    family: str = ""
    # The semantic classes the producing fact recorded at emission (it owns the site's meaning).
    semantic: tuple[str, ...] = ()
    # The slot in ``__defaults__`` for a DEFAULTS mutant (structured; the operator string never holds it).
    default_index: int | None = None
    # The key in ``__kwdefaults__`` for a DEFAULTS mutant of a keyword-only default; ``default_index`` is then None.
    default_name: str | None = None
    # A size-preserving bytecode edit, declared by the producing finder; None means the plugin recompiles.
    bc: BcEdit | None = None

    def __post_init__(self) -> None:
        if self.site_lineno == -1:
            object.__setattr__(self, "site_lineno", self.lineno)
        if self.site_end_lineno == -1:
            object.__setattr__(self, "site_end_lineno", self.site_lineno)


def non_body_node_ids(tree: ast.Module) -> frozenset[int]:
    """Ids of nodes outside any ``__code__``: defaults, annotations, function and class decorators.

    They are evaluated at definition time and live in ``__defaults__``/``__annotations__`` or are applied from
    outside. Patching a function body cannot reach them, so their mutants would falsely survive."""
    headers: list[ast.expr] = []
    for node in ast.walk(tree):
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                args = node.args
                headers.extend(d for d in [*args.defaults, *args.kw_defaults] if d is not None)
                every_arg = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
                headers.extend(a.annotation for a in every_arg if a is not None and a.annotation is not None)
                if node.returns is not None:
                    headers.append(node.returns)
                headers.extend(node.decorator_list)
            case ast.ClassDef():
                # A class decorator runs when the class is created, at import time: nothing in-memory can patch
                # it, and its line has no covering test (the import happens before coverage starts).
                headers.extend(node.decorator_list)
            case ast.Lambda():
                headers.extend(d for d in [*node.args.defaults, *node.args.kw_defaults] if d is not None)
            case ast.AnnAssign():
                # A local variable annotation is never evaluated; the assigned value is mutated separately.
                headers.append(node.annotation)
            case _:
                pass
    return frozenset(id(sub) for header in headers for sub in ast.walk(header))


def apply_mutant(source: str, mutant: Mutant) -> str:
    """Apply a mutant to the module text (byte-precise splice)."""
    data = source.encode("utf-8")
    patched = data[: mutant.start_byte] + mutant.mutated.encode("utf-8") + data[mutant.end_byte :]
    return patched.decode("utf-8")


def is_none_constant(node: ast.expr) -> bool:
    match node:
        case ast.Constant(value=None):
            return True
        case _:
            return False
