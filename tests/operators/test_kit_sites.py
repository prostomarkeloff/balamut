"""Mutation-sensitive coverage for the operator kit's matchers, finders and indent helpers.

The matchers are frozen predicates: each ``matches`` branch (including its negative paths) is
pinned so a flipped boolean/operator or a lost ``return`` is observed. The finders are pinned
through their exact emitted rows, including the boundary and non-matching shapes.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

import pytest

from balamut.core.mutants import Mutant, apply_mutant
from balamut.core.source_spans import line_byte_starts, node_byte_span
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import (
    AttrSwap,
    CallOf,
    ConstOf,
    DropEachArgument,
    DropKeyword,
    HoistFirstSuiteStatement,
    Inside,
    Named,
    NodeInSet,
    RegexRuleSite,
    Rewrite,
    SiteFinder,
    SwapCallArgs,
    ValueOf,
    assignment_target,
    attribute,
    call_of,
    const_of,
    context,
    expr_value,
    in_ids,
    inside,
    named,
    node_is,
    non_empty_collection,
    result_is_used,
    return_has_value,
    unary_op,
)


def _applied(source: str, kind: OperatorKind) -> list[str]:
    return [apply_mutant(source, mutant) for mutant in enumerate_facts("t.py", source, (kind,))]


def _rows(mutants: list[Mutant]) -> list[tuple[int, int, str, str, str]]:
    return [(m.start_byte, m.end_byte, m.original, m.mutated, m.operator) for m in mutants]


# --- Matchers: negative branches -------------------------------------------------


def test_named_and_attribute_negative_matches() -> None:
    source = "x = 1\ny = a.b\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    names = [n for n in ast.walk(tree) if isinstance(n, ast.Name)]
    consts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    attrs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)]

    assert named("other").matches(names[0], ctx) is False
    assert named("x").matches(consts[0], ctx) is False
    assert attribute("c").matches(attrs[0], ctx) is False
    assert attribute("b").matches(names[0], ctx) is False


def test_non_empty_collection_negative_matches() -> None:
    source = "d = {}\nx = 1\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    empty = next(n for n in ast.walk(tree) if isinstance(n, ast.Dict))
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))

    assert non_empty_collection().matches(empty, ctx) is False
    assert non_empty_collection().matches(name, ctx) is False


def test_call_of_negative_branches() -> None:
    source = "r = round(x, 2)\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "x")

    assert call_of(func="round").matches(name, ctx) is False  # not a Call
    assert call_of(receiver="a").matches(call, ctx) is False  # callee is a Name
    assert call_of(attr="b").matches(call, ctx) is False  # callee is a Name

    source2 = "r = a.b()\n"
    tree2 = ast.parse(source2)
    ctx2 = context("t.py", source2, tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))

    assert call_of(receiver="x").matches(call2, ctx2) is False  # receiver id mismatch
    assert call_of(attr="c").matches(call2, ctx2) is False  # attr mismatch


def test_call_of_min_args_defaults_to_zero() -> None:
    tree = ast.parse("r = g()\n")
    ctx = context("t.py", "r = g()\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert CallOf(func="g").matches(call, ctx) is True


def test_call_of_factory_forwards_min_args() -> None:
    # The `call_of` factory threads ``min_args`` into the built CallOf: a zero-arg call must not
    # match when the caller asked for ``min_args=1``.
    tree = ast.parse("g()\n")
    ctx = context("t.py", "g()\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert call_of(func="g", min_args=1).matches(call, ctx) is False

    tree2 = ast.parse("g(1)\n")
    ctx2 = context("t.py", "g(1)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    assert call_of(func="g", min_args=1).matches(call2, ctx2) is True


def test_const_of_negative_and_defaults() -> None:
    tree = ast.parse("x = 1\ns = ''\n")
    ctx = context("t.py", "x = 1\ns = ''\n", tree=tree)
    ints = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, int)]
    strs = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))

    assert const_of(str).matches(name, ctx) is False  # not a Constant
    assert const_of(str).matches(ints[0], ctx) is False  # type mismatch
    assert const_of(str, non_empty=True).matches(strs[0], ctx) is False  # empty string
    assert ConstOf(type_=str).matches(strs[0], ctx) is True  # non_empty defaults False


def test_result_is_used_call_argument_shapes() -> None:
    # Single positional argument (Is->IsNot, Or->And, return None).
    tree = ast.parse("g(1)\n")
    ctx = context("t.py", "g(1)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert result_is_used().matches(call.args[0], ctx) is True

    # Two positional arguments (any->all on args).
    tree3 = ast.parse("g(1, 2)\n")
    ctx3 = context("t.py", "g(1, 2)\n", tree=tree3)
    call3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.Call))
    assert result_is_used().matches(call3.args[0], ctx3) is True


def test_result_is_used_keyword_value_shapes() -> None:
    # A keyword value is a direct argument of the call (its ``keyword`` wrapper is elided).
    # Two keywords: ``any(keyword.value is node)`` -> ``all`` flips the result (line 253).
    tree = ast.parse("g(x=1, y=2)\n")
    ctx = context("t.py", "g(x=1, y=2)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    const = call.keywords[0].value
    assert ctx.parents is not None
    ctx.parents[id(const)] = call
    assert result_is_used().matches(const, ctx) is True

    # Single keyword: ``keyword.value is node`` -> ``is not`` flips the result (line 254).
    tree2 = ast.parse("g(x=1)\n")
    ctx2 = context("t.py", "g(x=1)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    const2 = call2.keywords[0].value
    assert ctx2.parents is not None
    ctx2.parents[id(const2)] = call2
    assert result_is_used().matches(const2, ctx2) is True


def test_result_is_used_binop_and_condition_shapes() -> None:
    tree = ast.parse("a + b\n")
    ctx = context("t.py", "a + b\n", tree=tree)
    binop = next(n for n in ast.walk(tree) if isinstance(n, ast.BinOp))
    assert result_is_used().matches(binop.left, ctx) is True
    assert result_is_used().matches(binop.right, ctx) is True

    tree2 = ast.parse("if c:\n    pass\n")
    ctx2 = context("t.py", "if c:\n    pass\n", tree=tree2)
    test = next(n for n in ast.walk(tree2) if isinstance(n, ast.If)).test
    assert result_is_used().matches(test, ctx2) is True

    tree3 = ast.parse("h()\n")
    ctx3 = context("t.py", "h()\n", tree=tree3)
    call3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.Call))
    assert result_is_used().matches(call3, ctx3) is False  # bare statement


def test_inside_matches_and_factory() -> None:
    source = "def f():\n    if a:\n        b = 1\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    inner = next(n for n in ast.walk(tree) if isinstance(n, ast.Constant))

    assert inside(ast.If).matches(inner, ctx) is True
    assert inside(ast.While).matches(inner, ctx) is False
    assert isinstance(inside(ast.If), Inside)


def test_assignment_target_shapes() -> None:
    tree = ast.parse("x = 1\n")
    ctx = context("t.py", "x = 1\n", tree=tree)
    assert assignment_target().matches(tree, ctx) is False  # module root (parent None)

    tree2 = ast.parse("x = y = 1\n")
    ctx2 = context("t.py", "x = y = 1\n", tree=tree2)
    assign = next(n for n in ast.walk(tree2) if isinstance(n, ast.Assign))
    assert assignment_target().matches(assign.targets[0], ctx2) is True  # multi-target any

    tree3 = ast.parse("a: int = 1\n")
    ctx3 = context("t.py", "a: int = 1\n", tree=tree3)
    ann = next(n for n in ast.walk(tree3) if isinstance(n, ast.AnnAssign))
    assert assignment_target().matches(ann.target, ctx3) is True  # AnnAssign target

    tree4 = ast.parse("return x\n")
    ctx4 = context("t.py", "return x\n", tree=tree4)
    ret = next(n for n in ast.walk(tree4) if isinstance(n, ast.Return))
    ret_value = ret.value
    assert ret_value is not None
    assert assignment_target().matches(ret_value, ctx4) is False  # other parent


def test_node_in_set_matches() -> None:
    tree = ast.parse("x = 1\n")
    ctx = context("t.py", "x = 1\n", tree=tree)
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))

    assert in_ids(frozenset({id(name)})).matches(name, ctx) is True
    assert in_ids(frozenset()).matches(name, ctx) is False
    assert isinstance(in_ids(frozenset()), NodeInSet)


def test_value_of_default_through_await() -> None:
    tree = ast.parse("async def f(q):\n    await q.join()\n")
    ctx = context("t.py", "async def f(q):\n    await q.join()\n", tree=tree)
    stmt = next(n for n in ast.walk(tree) if isinstance(n, ast.Expr))
    assert ValueOf(call_of(attr=frozenset({"join"}))).matches(stmt, ctx) is True


def test_expr_value_rejects_non_expr() -> None:
    tree = ast.parse("x = q.join()\n")
    ctx = context("t.py", "x = q.join()\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert expr_value(node_is(ast.Call)).matches(call, ctx) is False


# --- Finders: non-matching and boundary shapes ---------------------------------


def test_attr_swap_negative() -> None:
    tree = ast.parse("round(x)\n")
    ctx = context("t.py", "round(x)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert list(AttrSwap(swaps=(("a", "b"),)).find(call, ctx)) == []


def test_set_first_literal_arg_branches() -> None:
    from balamut.operators.kit import SetFirstLiteralArg

    tree = ast.parse("time.sleep(5)\n")
    ctx = context("t.py", "time.sleep(5)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert len(list(SetFirstLiteralArg(text="0").find(call, ctx))) == 1  # default index 0

    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(SetFirstLiteralArg(text="0").find(name, ctx)) == []  # non-Call

    tree2 = ast.parse("clamp(5)\n")
    ctx2 = context("t.py", "clamp(5)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    assert list(SetFirstLiteralArg(text="0", index=1).find(call2, ctx2)) == []  # boundary


def test_regex_rule_site_branches() -> None:
    tree = ast.parse("re.match('aaa', s)\n")
    ctx = context("t.py", "re.match('aaa', s)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))

    site = RegexRuleSite(rules=(("a", "b"),))
    assert len(list(site.find(call, ctx))) == 1  # default index 0
    assert next(iter(site.find(call, ctx)))[1] == "'baa'"  # count=1

    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(RegexRuleSite(rules=(("a", "b"),)).find(name, ctx)) == []  # non-Call

    tree2 = ast.parse("re.match('x')\n")
    ctx2 = context("t.py", "re.match('x')\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    assert list(RegexRuleSite(rules=(("a", "b"),), index=1).find(call2, ctx2)) == []  # boundary

    tree3 = ast.parse("re.match(pat)\n")
    ctx3 = context("t.py", "re.match(pat)\n", tree=tree3)
    call3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.Call))
    assert list(RegexRuleSite(rules=(("a", "b"),)).find(call3, ctx3)) == []  # pattern not Constant


def test_drop_argument_branches() -> None:
    from balamut.operators.kit import DropArgument

    tree = ast.parse("clamp(x, 0, 100)\n")
    ctx = context("t.py", "clamp(x, 0, 100)\n", tree=tree)
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(DropArgument(index=1).find(name, ctx)) == []  # non-Call

    tree2 = ast.parse("clamp(5)\n")
    ctx2 = context("t.py", "clamp(5)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    assert list(DropArgument(index=1).find(call2, ctx2)) == []  # boundary

    # last-argument drop takes the else branch (lines 520/523).
    rows = _rows(
        enumerate_facts(
            "t.py",
            "def f():\n    return clamp(x, 0, 100)\n",
            ((Rewrite(match=call_of(func="clamp"), transform=DropArgument(index=2), operator="p:x"),),),
        )
    )
    assert rows[0][2] == ", 100"


def test_separator_before_variants() -> None:
    from balamut.operators.kit import _separator_before

    def sep(source: str, name_start: int) -> int | None:
        ctx = EnumContext("t.py", source.encode("utf-8"), line_byte_starts(source), frozenset())
        return _separator_before(ctx, name_start)

    # Comma directly precedes the keyword (no whitespace): `name_start - 1` lands on it.
    assert sep("f(a,timeout=5)", 4) == 3
    # No comma at all.
    assert sep("f(timeout=5)", 2) is None
    # Comma at byte 0.
    assert sep(",timeout=5", 1) == 0


def test_scale_literal_branches() -> None:
    from balamut.operators.kit import ScaleLiteral

    tree = ast.parse("round(x, 2)\n")
    ctx = context("t.py", "round(x, 2)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert len(list(ScaleLiteral(scale=10).find(call, ctx))) == 1  # default index 1

    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(ScaleLiteral(scale=10).find(name, ctx)) == []  # non-Call

    tree2 = ast.parse("round(x)\n")
    ctx2 = context("t.py", "round(x)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    assert list(ScaleLiteral(scale=10, index=1).find(call2, ctx2)) == []  # boundary

    tree3 = ast.parse("f(x)\n")
    ctx3 = context("t.py", "f(x)\n", tree=tree3)
    call3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.Call))
    assert list(ScaleLiteral(scale=10, index=0).find(call3, ctx3)) == []  # non-Constant arg


def test_flip_bool_negative() -> None:
    from balamut.operators.kit import FlipBool

    tree = ast.parse("f(x)\n")
    ctx = context("t.py", "f(x)\n", tree=tree)
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(FlipBool().find(name, ctx)) == []


def test_swap_call_args_branches() -> None:
    tree = ast.parse("g(a)\n")
    ctx = context("t.py", "g(a)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(SwapCallArgs(first=0, second=1).find(name, ctx)) == []  # non-Call
    assert list(SwapCallArgs(first=0, second=1).find(call, ctx)) == []  # boundary / max


def test_naked_receiver_negative() -> None:
    from balamut.operators.kit import NakedReceiver

    tree = ast.parse("g()\n")
    ctx = context("t.py", "g()\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert list(NakedReceiver().find(call, ctx)) == []


def test_drop_each_argument_negative() -> None:
    tree = ast.parse("g(a)\n")
    ctx = context("t.py", "g(a)\n", tree=tree)
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert list(DropEachArgument().find(name, ctx)) == []


def test_hoist_ignores_non_with() -> None:
    tree = ast.parse("if ok:\n    a()\n    b()\n")
    ctx = context("t.py", "if ok:\n    a()\n    b()\n", tree=tree)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.If))
    assert list(HoistFirstSuiteStatement().regions(node, ctx)) == []


# --- Indent helpers ------------------------------------------------------------


def test_line_indent_odd_and_boundary() -> None:
    from balamut.operators.kit import line_indent

    tree = ast.parse("if ok:\n   pass\n")
    ctx = context("t.py", "if ok:\n   pass\n", tree=tree)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Pass))
    assert line_indent(ctx, node) == "   "  # odd indent

    src = "x = 1\n    "
    ctx2 = EnumContext("t.py", src.encode("utf-8"), line_byte_starts(src), frozenset())
    node2 = ast.parse("pass").body[0]
    node2.lineno = 2
    assert line_indent(ctx2, node2) == "    "  # indentation runs to end of source


def test_reindent_branches() -> None:
    from balamut.operators.kit import reindent

    assert reindent("", "  ", first=False) == ""
    assert reindent("a\nb\nc", "  ", first=False) == "a\n  b\n  c\n"


# --- Drop keyword and its span helpers ------------------------------------------


def test_drop_keyword_multiple_keywords() -> None:
    # The first keyword does not match: the `continue` keeps scanning (line 568).
    source = "def f():\n    return fetch(url, a=1, timeout=5)\n"
    kind = (Rewrite(match=call_of(min_args=0), transform=DropKeyword(name="timeout"), operator="p:x"),)
    assert _applied(source, kind) == ["def f():\n    return fetch(url, a=1)\n"]


def test_drop_keyword_repeated_keyword() -> None:
    # The yield is followed by `continue`, not `break` (line 579): a later keyword is still dropped.
    source = "def f():\n    return fetch(url, timeout=5, timeout=6)\n"
    kind = (Rewrite(match=call_of(min_args=0), transform=DropKeyword(name="timeout"), operator="p:x"),)
    assert len(_applied(source, kind)) == 2


def test_drop_keyword_first_with_next_keyword() -> None:
    # The keyword is first and a later keyword follows: `next_start` branch (lines 546-548, 581).
    source = "def f():\n    return fetch(timeout=5, b=2)\n"
    kind = (Rewrite(match=call_of(min_args=0), transform=DropKeyword(name="timeout"), operator="p:x"),)
    assert _applied(source, kind) == ["def f():\n    return fetch(b=2)\n"]


def test_drop_keyword_unpositioned_value() -> None:
    # A keyword whose value has no byte span is skipped (`continue`, line 571), not a hard stop.
    source = "def f():\n    return fetch(url, timeout=5, timeout=6)\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    call.keywords[0].value.end_lineno = None
    call.keywords[0].value.end_col_offset = None
    assert len(list(DropKeyword(name="timeout").find(call, ctx))) == 1


def test_previous_argument_end_keyword_branch() -> None:
    from balamut.operators.kit import _previous_argument_end

    tree = ast.parse("f(a=1, timeout=5)\n")
    ctx = context("t.py", "f(a=1, timeout=5)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    span = node_byte_span(call.keywords[0].value, ctx.line_starts)
    assert span is not None
    expected = span[1]
    assert _previous_argument_end(call, 1, ctx) == expected


def test_previous_argument_end_index_boundary() -> None:
    from balamut.operators.kit import _previous_argument_end

    # index 0 with positional args takes the args branch (line 536 `index > 0` is False).
    tree = ast.parse("f(a, timeout=5)\n")
    ctx = context("t.py", "f(a, timeout=5)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    span = node_byte_span(call.args[-1], ctx.line_starts)
    assert span is not None
    expected = span[1]
    assert _previous_argument_end(call, 0, ctx) == expected

    # index 1 with no positional args takes the keyword branch (line 536 `index > 0` is True).
    tree2 = ast.parse("f(a=1, timeout=5)\n")
    ctx2 = context("t.py", "f(a=1, timeout=5)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    span2 = node_byte_span(call2.keywords[0].value, ctx2.line_starts)
    assert span2 is not None
    expected2 = span2[1]
    assert _previous_argument_end(call2, 1, ctx2) == expected2


def test_next_argument_start_keyword_branch() -> None:
    from balamut.operators.kit import _next_argument_start

    tree = ast.parse("f(timeout=5, b=2)\n")
    ctx = context("t.py", "f(timeout=5, b=2)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    expected = ctx.line_starts[call.keywords[1].lineno - 1] + call.keywords[1].col_offset
    assert _next_argument_start(call, 0, ctx) == expected

    tree2 = ast.parse("f(a=1, b=2)\n")
    ctx2 = context("t.py", "f(a=1, b=2)\n", tree=tree2)
    call2 = next(n for n in ast.walk(tree2) if isinstance(n, ast.Call))
    expected2 = ctx2.line_starts[call2.keywords[1].lineno - 1] + call2.keywords[1].col_offset
    assert _next_argument_start(call2, 0, ctx2) == expected2

    tree3 = ast.parse("f(a=1, b=2, c=3)\n")
    ctx3 = context("t.py", "f(a=1, b=2, c=3)\n", tree=tree3)
    call3 = next(n for n in ast.walk(tree3) if isinstance(n, ast.Call))
    expected3 = ctx3.line_starts[call3.keywords[2].lineno - 1] + call3.keywords[2].col_offset
    assert _next_argument_start(call3, 1, ctx3) == expected3


def test_next_argument_start_boundary() -> None:
    from balamut.operators.kit import _next_argument_start

    # No next keyword and no positional args: returns None (lines 546, 552).
    tree = ast.parse("f(timeout=5)\n")
    ctx = context("t.py", "f(timeout=5)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _next_argument_start(call, 0, ctx) is None


def test_next_argument_start_args_branch() -> None:
    from balamut.operators.kit import _next_argument_start

    # index past the keywords, positional args exist: args branch (lines 550-551).
    tree = ast.parse("f(a, timeout=5)\n")
    ctx = context("t.py", "f(a, timeout=5)\n", tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    span = node_byte_span(call.args[0], ctx.line_starts)
    assert span is not None
    expected = span[0]
    assert _next_argument_start(call, 0, ctx) == expected


def test_operator_factories() -> None:
    from balamut.operators.kit import ReturnHasValue, UnaryOpOf

    assert isinstance(unary_op(ast.Not), UnaryOpOf)
    assert isinstance(return_has_value(), ReturnHasValue)
    assert isinstance(named("x"), Named)


def test_constant_type_union() -> None:
    from balamut.operators.kit import ConstantType

    assert ConstantType.__value__ == type[str | int | float | bool]


def test_attr_name_span_guard() -> None:
    from balamut.operators.kit import attr_name_span

    attr = ast.Attribute(value=ast.Name(id="a", ctx=ast.Load()), attr="b", ctx=ast.Load())
    attr.lineno = 1
    attr.col_offset = 0
    attr.end_lineno = None  # only one position field is missing
    attr.end_col_offset = 5
    ctx = EnumContext("t.py", b"a.b", [0], frozenset())
    assert attr_name_span(attr, ctx) is None


def test_region_rewrite_skips_unpositioned_target() -> None:
    from balamut.operators.kit import RegionRewrite

    class _FakeRegions:
        def regions(self, node: ast.AST, ctx: EnumContext) -> Iterator[tuple[ast.expr | ast.stmt, str]]:
            bad = ast.Pass()
            bad.lineno = 1
            bad.col_offset = 0
            bad.end_lineno = None  # no byte span
            bad.end_col_offset = 0
            yield bad, "x"
            assert isinstance(node, ast.expr | ast.stmt)
            yield node, "y"

    kind = RegionRewrite(match=node_is(ast.If), transform=_FakeRegions(), operator="x")
    assert len(kind.enumerate("t.py", "if a:\n    pass\n")) == 1


def test_region_rewrite_records_itself_as_the_fact() -> None:
    from balamut.operators.kit import RegionRewrite

    class _ReplaceIf:
        def regions(self, node: ast.AST, ctx: EnumContext) -> Iterator[tuple[ast.expr | ast.stmt, str]]:
            assert isinstance(node, ast.expr | ast.stmt)
            yield node, "y"

    kind = RegionRewrite(match=node_is(ast.If), transform=_ReplaceIf(), operator="x")
    mutants = kind.enumerate("t.py", "if a:\n    pass\n")
    assert mutants
    assert all(mutant.fact is kind for mutant in mutants)


# --- Finders: hosts they do not own, unpositioned arguments, the operator contract ---------


def _name_node(source: str = "x\n") -> tuple[ast.Name, EnumContext]:
    tree = ast.parse(source)
    return next(n for n in ast.walk(tree) if isinstance(n, ast.Name)), context("t.py", source, tree=tree)


def test_table_finders_yield_nothing_for_a_node_of_another_type() -> None:
    from balamut.operators.kit import (
        AugAssignSwapSite,
        BinaryOperand,
        BinopSwapSite,
        BoolopSwapSite,
        CompareSwapSite,
        DropEachKeyword,
        SwapAdjacentArgs,
    )

    name, ctx = _name_node()
    assert list(DropKeyword(name="timeout").find(name, ctx)) == []
    assert list(DropEachKeyword().find(name, ctx)) == []
    assert list(SwapAdjacentArgs().find(name, ctx)) == []
    assert list(BinaryOperand(side="left").find(name, ctx)) == []
    assert list(CompareSwapSite(((ast.Lt, ast.LtE),)).find(name, ctx)) == []
    assert list(BinopSwapSite(((ast.Add, ast.Sub),)).find(name, ctx)) == []
    assert list(AugAssignSwapSite(((ast.Add, ast.Sub),)).find(name, ctx)) == []
    assert list(BoolopSwapSite(((ast.And, ast.Or),)).find(name, ctx)) == []


def test_number_shift_site_ignores_a_constant_that_is_not_a_number() -> None:
    from balamut.operators.kit import NumberShiftSite

    source = "x = 'text'\ny = None\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    for constant in (n for n in ast.walk(tree) if isinstance(n, ast.Constant)):
        assert list(NumberShiftSite(1).find(constant, ctx)) == []


def _call_with_unpositioned_argument(source: str, index: int) -> tuple[ast.Call, EnumContext]:
    tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    call.args[index].end_lineno = None
    return call, context("t.py", source, tree=tree)


def test_argument_drops_skip_a_call_with_an_unpositioned_argument() -> None:
    # A separator splice needs the byte span of every argument: one argument without it voids the whole call.
    from balamut.operators.kit import DropArgument

    call, ctx = _call_with_unpositioned_argument("clamp(x, 0, 100)\n", 2)
    assert list(DropArgument(index=0).find(call, ctx)) == []
    assert list(DropEachArgument().find(call, ctx)) == []


def test_drop_each_keyword_skips_only_the_unpositioned_value() -> None:
    from balamut.operators.kit import DropEachKeyword

    source = "fetch(url, a=1, b=2, c=3)\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    call.keywords[0].value.end_lineno = None

    spans = [span for _target, _text, span, _operator, _bc in DropEachKeyword().find(call, ctx) if span is not None]

    assert [source[:start] + source[end:] for start, end in spans] == [
        "fetch(url, a=1, c=3)\n",
        "fetch(url, a=1, b=2)\n",
    ]


def test_rewrite_rejects_a_site_without_an_operator_when_the_fact_names_none() -> None:
    from balamut.operators.kit import BinaryOperand

    kind = (Rewrite(match=node_is(ast.BinOp), transform=BinaryOperand(side="left")),)
    with pytest.raises(ValueError, match="Rewrite: a site yielded no operator and the fact names none"):
        enumerate_facts("t.py", "def f():\n    return a + b\n", (kind,))


def _spliced(source: str, finder: SiteFinder) -> list[str]:
    """The source after each splice the finder yields over the first call of ``source``."""
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    spans = [span for _target, _text, span, _operator, _bc in finder.find(call, ctx) if span is not None]
    return [source[:start] + source[end:] for start, end in spans]


def test_drop_each_keyword_drops_every_named_keyword_and_leaves_a_splat() -> None:
    from balamut.operators.kit import DropEachKeyword

    assert _spliced("fetch(**extra, a=1, b=2)\n", DropEachKeyword()) == [
        "fetch(**extra, b=2)\n",
        "fetch(**extra, a=1)\n",
    ]


def test_swap_adjacent_args_skips_an_identical_pair_and_goes_on() -> None:
    from balamut.operators.kit import SwapAdjacentArgs

    source = "g(a, a, b)\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))

    assert [text for _target, text, _span, _operator, _bc in SwapAdjacentArgs().find(call, ctx)] == ["g(a, b, a)"]


def test_assignment_target_matches_a_part_of_the_target() -> None:
    # With a parent map that links an expression to its statement, a name inside the target is part of it.
    source = "a, b = pair\nobj.field: int = 1\ntotal[key] += 1\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    assert ctx.parents is not None
    for statement, name in zip(tree.body, ("a", "obj", "total"), strict=True):
        part = next(n for n in ast.walk(statement) if isinstance(n, ast.Name) and n.id == name)
        ctx.parents[id(part)] = statement
        assert assignment_target().matches(part, ctx) is True
