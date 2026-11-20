"""The ``src`` kind: kit compositions over the decision-logic mutation tables.

These tests pin the matchers and the module-level fact compositions: a header node (default,
annotation or decorator) is outside any ``__code__`` and must not match, and the swap/drop
filters must stay as strict as the tables declare.
"""

from __future__ import annotations

import ast

import pytest

from balamut.operators import src as src_module
from balamut.operators.facts import EnumContext
from balamut.operators.kit import context
from balamut.operators.src import (
    _BINOP_SWAP,
    BodySite,
    SwappableBinop,
    body_site,
    default_position,
    enumerate_defaults,
    enumerate_mutants,
)
from bm_support.modules import reloaded


def _ops(source: str) -> set[str]:
    return {mutant.operator for mutant in enumerate_mutants("t.py", source)}


def test_body_site_factory() -> None:
    assert isinstance(body_site(), BodySite)


def test_default_position_negative_branches() -> None:
    # A non-literal default (line 84).
    tree = ast.parse("def f(x=compute()):\n    return x\n")
    ctx = context("t.py", "def f(x=compute()):\n    return x\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert default_position().matches(call, ctx) is False

    # A constant whose parent is not ``arguments`` (line 87).
    tree2 = ast.parse("def f():\n    return 1\n")
    ctx2 = context("t.py", "def f():\n    return 1\n", tree=tree2)
    const2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Constant))
    assert default_position().matches(const2, ctx2) is False

    # A keyword-only default is not in ``args.defaults`` (line 89).
    tree3 = ast.parse("def f(*, x=1):\n    return x\n")
    ctx3 = context("t.py", "def f(*, x=1):\n    return x\n", tree=tree3)
    func3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.FunctionDef))
    kw_default = func3.args.kw_defaults[0]
    assert kw_default is not None
    assert default_position().matches(kw_default, ctx3) is False

    # A lambda's default is not reachable (line 92).
    tree4 = ast.parse("f = lambda x=1: x\n")
    ctx4 = context("t.py", "f = lambda x=1: x\n", tree=tree4)
    lam = next(n for n in ast.walk(tree4) if isinstance(n, ast.Lambda))
    assert default_position().matches(lam.args.defaults[0], ctx4) is False

    # A nested function's default is not reachable (line 96).
    tree5 = ast.parse("def o():\n    def i(x=1):\n        return x\n    return i\n")
    ctx5 = context("t.py", "def o():\n    def i(x=1):\n        return x\n    return i\n", tree=tree5)
    inner = next(n for n in ast.walk(tree5) if isinstance(n, ast.FunctionDef) and n.name == "i")
    assert default_position().matches(inner.args.defaults[0], ctx5) is False


def test_swappable_binop_branches() -> None:
    swappable = SwappableBinop(_BINOP_SWAP)

    tree = ast.parse("x + 1\n")
    ctx = context("t.py", "x + 1\n", tree=tree)
    binop = next(n for n in ast.walk(tree) if isinstance(n, ast.BinOp))
    assert swappable.matches(binop, ctx) is True  # `type(op) is from_`

    tree2 = ast.parse("x + 0\n")
    ctx2 = context("t.py", "x + 0\n", tree=tree2)
    binop2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.BinOp))
    assert swappable.matches(binop2, ctx2) is False  # zero-numeric right

    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert swappable.matches(name, ctx) is False  # not a BinOp/AugAssign

    # An operator outside the swap table (MatMult) is not swappable (line 125).
    tree3 = ast.parse("a @ b\n")
    ctx3 = context("t.py", "a @ b\n", tree=tree3)
    matmul = next(n for n in ast.walk(tree3) if isinstance(n, ast.BinOp))
    assert swappable.matches(matmul, ctx3) is False


def test_defaults_covering_line_branches() -> None:
    # A docstring pushes the covering line to the first body statement.
    muts = enumerate_defaults("t.py", 'def f(x=1):\n    """doc"""\n    return x\n')
    assert muts
    assert all(m.lineno == 3 for m in muts)
    # A body that is only a docstring covers the docstring line itself.
    muts2 = enumerate_defaults("t.py", 'def g(x=1):\n    """doc"""\n')
    assert muts2
    assert all(m.lineno == 2 for m in muts2)


def test_defaults_continue_skips_nonliteral() -> None:
    # A non-literal default is skipped, the literal one is still processed (line 274).
    muts = enumerate_defaults("t.py", "def f(a=compute(), b=2):\n    return a\n")
    assert {m.operator for m in muts} == {"lang:const:num:2->3", "lang:const:num:2->1"}


def test_enumerate_defaults_stamps_the_src_family() -> None:
    # ``family=FAMILY`` labels the defaults phase mutants with the kind; dropping it leaves "".
    muts = enumerate_defaults("t.py", "def f(x=1):\n    return x\n")
    assert muts
    assert all(m.family == "src" for m in muts)


def test_defaults_skips_opless_finder_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    # A finder yield with no operator name is skipped, not a hard stop (line 278).
    import balamut.operators.src as src_mod
    from balamut.operators.src import DefaultsFact

    class _OpLessFinder:
        def __init__(self, delta: int = 0) -> None:
            self.delta = delta

        def find(self, node: ast.AST, ctx: EnumContext):
            yield node, "9", None, None, None
            yield node, "8", None, "lang:const:num:1->2", None

    monkeypatch.setattr(src_mod, "_DEFAULT_FINDERS", (_OpLessFinder(),))
    muts = DefaultsFact().enumerate("t.py", "def f(x=1):\n    return x\n")
    assert [m.operator for m in muts] == ["lang:const:num:1->2"]


def test_src_kind_composition_excludes_header_nodes() -> None:
    # Every fact composes ``body_site() & X``: a node in a default/decorator header must not match.
    src = (
        "@decorator(1 < 2, 1, True)\n"
        "def f(x=a + b, y=p and q, z=not r, w=max(u, v), v=s.find('x'), u=round(v, 2), t=d.quantize(e)):\n"
        "    return x\n"
    )
    assert _ops(src) == {"lang:return:None"}


def test_src_kind_number_shift_and_const_union() -> None:
    # ``const_of(int) | const_of(float)`` stays a union; NumberShiftSite keeps delta 1.
    assert "lang:const:num:1->2" in _ops("def f():\n    return 1\n")


def test_src_kind_augassign_swappable_filter() -> None:
    # ``node_is(AugAssign) & SwappableBinop`` still suppresses a zero-numeric right operand.
    ops = _ops("def f(a):\n    a += 0\n    return a\n")
    assert not {op for op in ops if op.startswith("lang:augassign")}


def test_src_kind_delete_call_composition() -> None:
    # Only an expression statement whose value is a call matches the delete-call fact.
    assert "lang:delete:call" not in _ops("def f(x):\n    x\n    return x\n")


def test_src_kind_return_none_composition() -> None:
    # Only a return with a value matches the return-None fact.
    assert "lang:return:None" not in _ops("def f():\n    x = 1\n")


def test_src_kind_break_continue_composition() -> None:
    # A body without break/continue produces neither control mutant.
    ops = _ops("def f():\n    return 1\n")
    assert "lang:control:break->continue" not in ops
    assert "lang:control:continue->break" not in ops


def test_call_argument_operators_enumerate() -> None:
    # Adjacent swaps for every pair, a drop per argument, a keyword drop per keyword.
    source = "def f():\n    return g(a, b, c, mode=1)\n"
    ops = _ops(source)
    assert "lang:call:arg-swap" in ops
    assert "lang:call:arg-drop" in ops
    assert "lang:call:kwarg-drop" in ops
    mutants = enumerate_mutants("t.py", source)
    swaps = [m for m in mutants if m.operator == "lang:call:arg-swap"]
    assert len(swaps) == 2  # (a, b) and (b, c)
    drops = [m for m in mutants if m.operator == "lang:call:arg-drop"]
    assert len(drops) == 3  # a, b, c
    kwargs = [m for m in mutants if m.operator == "lang:call:kwarg-drop"]
    assert len(kwargs) == 1  # mode=1
    assert any("g(b, a, c" in m.mutated for m in swaps)


def test_src_kind_operator_names() -> None:
    # Every explicit operator name and namespace in the module-level table is pinned: a swap/drop
    # in ``operator_name(...)`` or ``namespace=...`` must surface as a wrong operator id.
    # A reload re-executes the module-level table inside the test body so the coverage tracer
    # attributes the lines to this test (a cold mutant would otherwise read as uncovered).
    with reloaded(src_module):
        assert "lang:unary:not-removal" in _ops("def f():\n    return not x\n")
        assert "lang:control:break->continue" in _ops("def f():\n    while True:\n        break\n")
        assert "lang:control:continue->break" in _ops("def f():\n    while True:\n        continue\n")
        assert "std:builtins:min->max" in _ops("def f(a, b):\n    return min(a, b)\n")
        assert "std:str:startswith->endswith" in _ops("def f(s, x):\n    return s.startswith(x)\n")
        assert "std:builtins:round-drop" in _ops("def f(x):\n    return round(x)\n")
        assert "std:decimal:quantize-drop" in _ops("def f(d, e):\n    return d.quantize(e)\n")
        assert "lang:return:None" in _ops("def f():\n    return 1\n")
        assert "lang:delete:call" in _ops("def f():\n    g()\n")


def test_src_kind_call_argument_operators_are_high_bucket() -> None:
    # The call-argument operators are declared triage=HIGH; dropping the kwarg falls back to REVIEW.
    from balamut.core.triage import Bucket, classify

    mutants = enumerate_mutants("t.py", "def f():\n    return g(a, b, mode=1)\n")
    call_arg = [m for m in mutants if m.operator.startswith("lang:call:arg")]
    assert call_arg
    for m in call_arg:
        assert classify(m) is Bucket.HIGH


def test_src_kind_bool_flip_is_high_bucket() -> None:
    # FlipBoolSite is declared triage=HIGH in the table; dropping the kwarg falls back to REVIEW.
    from balamut.core.triage import Bucket, classify

    with reloaded(src_module):
        mutants = src_module.enumerate_mutants("t.py", "def f():\n    return True\n")
        bool_flips = [m for m in mutants if m.operator.startswith("lang:const:bool")]
        assert bool_flips
        for m in bool_flips:
            assert classify(m) is Bucket.HIGH


def test_call_argument_operators_skip_no_ops() -> None:
    # Identical adjacent arguments swap to the same text; a splat is not a keyword drop target.
    mutants = enumerate_mutants("t.py", "def f():\n    return g(a, a, **extra)\n")
    assert [m for m in mutants if m.operator == "lang:call:arg-swap"] == []
    assert [m for m in mutants if m.operator == "lang:call:kwarg-drop"] == []
    drops = [m for m in mutants if m.operator == "lang:call:arg-drop"]
    assert len(drops) == 2  # a and a: both droppable, the texts differ by span


def test_call_argument_operators_respect_the_body_site() -> None:
    # A default-argument call is outside the function body.
    mutants = enumerate_mutants("t.py", "def f(x=g(a, b)):\n    return x\n")
    assert [m for m in mutants if m.operator.startswith("lang:call:arg")] == []
