"""The src operators beyond the one-swap tables: the sufficient relational set, forced conditions, dropped boolean
and arithmetic operands, literals, assigned values, handlers, splats, normalizers and every kind of default.

Each test pins both what an operator emits and what it must leave alone (an arid site, an equivalent swap, a
statement outside a function body)."""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable

from balamut.config import KindOverrides
from balamut.core.mutants import ApplyMechanism, apply_mutant
from balamut.core.triage import Bucket, classify
from balamut.engine.enumeration import resolve_kinds
from balamut.operators.facts import EnumContext, enumerate_facts, mutant_mechanism
from balamut.operators.kit import (
    DropBoolOperandSite,
    DropEachMappingSplat,
    KeepOperandSite,
    SignDropSite,
    StringLiteralSite,
    _size_equivalent,
    context,
)
from balamut.operators.src import (
    AridString,
    AssignedValue,
    ConditionTest,
    EnvironmentProbe,
    HandlerType,
    OperandOperation,
    ValueUsed,
    _arid_call,
    _arid_expression,
    _declares,
    _probes_environment,
    enumerate_defaults,
    enumerate_mutants,
)


def _changes(source: str, prefix: str) -> set[tuple[str, str, str]]:
    """(operator, original, mutated) of the mutants whose operator starts with ``prefix``."""
    return {
        (m.operator, m.original, m.mutated)
        for m in enumerate_mutants("t.py", textwrap.dedent(source))
        if m.operator.startswith(prefix)
    }


def _mutated(source: str, operator: str) -> list[str]:
    """The mutated module texts of one operator, every one of them valid Python."""
    source = textwrap.dedent(source)
    texts = [apply_mutant(source, m) for m in enumerate_mutants("t.py", source) if m.operator == operator]
    for text in texts:
        ast.parse(text)
    return texts


def _node(source: str, predicate: Callable[[ast.AST], bool]) -> tuple[ast.AST, EnumContext]:
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    return next(n for n in ast.walk(tree) if predicate(n)), ctx


# --- Relational operators ------------------------------------------------------------------------------------


def test_every_relational_operator_gets_its_sufficient_replacements() -> None:
    cases = {
        "a < b": {"a <= b", "a != b"},
        "a <= b": {"a < b", "a == b"},
        "a > b": {"a >= b", "a != b"},
        "a >= b": {"a > b", "a == b"},
        "a == b": {"a != b", "a <= b", "a >= b"},
        "a != b": {"a == b", "a < b", "a > b"},
    }
    for expression, expected in cases.items():
        source = f"def f(a, b):\n    return {expression}\n"
        found = {mutated for _op, _orig, mutated in _changes(source, "lang:compare")}
        assert found == expected, expression


def test_identity_and_membership_keep_their_negation_only() -> None:
    assert {m for _o, _r, m in _changes("def f(a, b):\n    return a is b\n", "lang:compare")} == {"a is not b"}
    assert {m for _o, _r, m in _changes("def f(a, b):\n    return a in b\n", "lang:compare")} == {"a not in b"}


def test_new_relational_operator_names() -> None:
    ops = {op for op, _o, _m in _changes("def f(a, b):\n    return a == b\n", "lang:compare")}
    assert ops == {"lang:compare:Eq->NotEq", "lang:compare:Eq->LtE", "lang:compare:Eq->GtE"}
    mutants = enumerate_mutants("t.py", "def f(a, b):\n    return a < b\n")
    mutant = next(m for m in mutants if m.operator.endswith("NotEq"))
    assert classify(mutant) is Bucket.HIGH
    assert mutant.bc is not None
    assert mutant.bc.target == "NotEq"


def test_a_size_compared_with_zero_skips_the_equivalent_swaps() -> None:
    # len(x) == 0 is len(x) <= 0, len(x) != 0 is len(x) > 0, on either side of the comparison.
    left = {m for _o, _r, m in _changes("def f(x):\n    return len(x) == 0\n", "lang:compare")}
    assert left == {"len(x) != 0", "len(x) >= 0"}
    left_ne = {m for _o, _r, m in _changes("def f(x):\n    return len(x) != 0\n", "lang:compare")}
    assert left_ne == {"len(x) == 0", "len(x) < 0"}
    left_gt = {m for _o, _r, m in _changes("def f(x):\n    return abs(x) > 0\n", "lang:compare")}
    assert left_gt == {"abs(x) >= 0"}
    left_le = {m for _o, _r, m in _changes("def f(x):\n    return len(x) <= 0\n", "lang:compare")}
    assert left_le == {"len(x) < 0"}
    right = {m for _o, _r, m in _changes("def f(x):\n    return 0 == len(x)\n", "lang:compare")}
    assert right == {"0 != len(x)", "0 <= len(x)"}
    right_ne = {m for _o, _r, m in _changes("def f(x):\n    return 0 != len(x)\n", "lang:compare")}
    assert right_ne == {"0 == len(x)", "0 > len(x)"}
    right_lt = {m for _o, _r, m in _changes("def f(x):\n    return 0 < len(x)\n", "lang:compare")}
    assert right_lt == {"0 <= len(x)"}
    right_ge = {m for _o, _r, m in _changes("def f(x):\n    return 0 >= len(x)\n", "lang:compare")}
    assert right_ge == {"0 > len(x)"}
    # Not a size, or not zero: every replacement stays.
    assert len(_changes("def f(x):\n    return count(x) == 0\n", "lang:compare")) == 3
    assert len(_changes("def f(x):\n    return len(x) == 1\n", "lang:compare")) == 3
    assert len(_changes("def f(x):\n    return len(x) == False\n", "lang:compare")) == 3
    assert len(_changes("def f(x):\n    return 0 == count(x)\n", "lang:compare")) == 3
    assert len(_changes("def f(x):\n    return 1 == len(x)\n", "lang:compare")) == 3
    # In a chain each operator compares its own neighbours: only the first link is a size against zero.
    chain = {m for _o, _r, m in _changes("def f(x, y):\n    return len(x) == 0 < y\n", "lang:compare")}
    assert "len(x) <= 0 < y" not in chain
    assert "len(x) == 0 != y" in chain
    size, zero, other = (ast.parse(text, mode="eval").body for text in ("len(x)", "0", "y"))
    assert _size_equivalent(size, other, (ast.Eq, ast.LtE)) is False
    assert _size_equivalent(size, zero, (ast.Eq, ast.NotEq)) is False
    assert _size_equivalent(zero, size, (ast.Eq, ast.LtE)) is False


def test_an_environment_probe_gets_no_new_replacements() -> None:
    found = {m for _o, _r, m in _changes("import sys\ndef f():\n    return sys.version_info >= (3, 13)\n", "lang:")}
    assert "sys.version_info == (3, 13)" not in found
    assert "sys.version_info > (3, 13)" in found  # the one-swap table is unchanged
    assert "2" not in found  # no lowered literal inside the probe
    assert not _changes("import sys\ndef f():\n    return sys.platform == 'win32'\n", "lang:const:str")


# --- Conditions ------------------------------------------------------------------------------------------------


def test_conditions_are_forced_to_either_constant() -> None:
    source = """
        def f(a, xs):
            if a:
                g()
            elif a > 1:
                h()
            y = 1 if a else 2
            return [x for x in xs if x if x > a]
    """
    true = {orig for _op, orig, _m in _changes(source, "lang:condition:True")}
    false = {orig for _op, orig, _m in _changes(source, "lang:condition:False")}
    assert true == {"a", "a > 1", "x", "x > a"}
    assert false == true
    for text in _mutated(source, "lang:condition:True"):
        assert "True" in text


def test_a_loop_condition_is_only_forced_to_false() -> None:
    source = "def f(a):\n    while a:\n        a -= 1\n"
    assert {(op, orig) for op, orig, _m in _changes(source, "lang:condition")} == {("lang:condition:False", "a")}


def test_constant_and_environment_conditions_are_not_forced() -> None:
    source = """
        import sys
        from typing import TYPE_CHECKING
        def f():
            while True:
                break
            if TYPE_CHECKING:
                pass
            if sys.version_info >= (3, 13) and sys.platform == "linux":
                pass
            if __name__ == "__main__":
                pass
    """
    assert not _changes(source, "lang:condition")
    assert not _changes(source, "lang:boolop:And-drop")


def test_a_module_level_condition_is_not_forced() -> None:
    assert not _changes("if FLAG:\n    x = 1\n", "lang:condition")


def test_condition_test_matcher_rejects_other_positions() -> None:
    node, ctx = _node("def f(a):\n    return a\n", lambda n: isinstance(n, ast.Name) and n.id == "a")
    assert ConditionTest(loops=True).matches(node, ctx) is False
    statement, ctx2 = _node("def f(a):\n    if a:\n        pass\n", lambda n: isinstance(n, ast.If))
    assert ConditionTest(loops=True).matches(statement, ctx2) is False
    body, ctx3 = _node("def f(a):\n    if a:\n        b\n", lambda n: isinstance(n, ast.Name) and n.id == "b")
    assert ConditionTest(loops=True).matches(body, ctx3) is False
    loop_body, ctx4 = _node("def f(a):\n    while a:\n        b\n", lambda n: isinstance(n, ast.Name) and n.id == "b")
    assert ConditionTest(loops=True).matches(loop_body, ctx4) is False
    element, ctx5 = _node("def f(a):\n    return [x for x in a]\n", lambda n: isinstance(n, ast.Name) and n.id == "a")
    assert ConditionTest(loops=True).matches(element, ctx5) is False
    statement, ctx6 = _node("def f(a):\n    while a:\n        b\n", lambda n: isinstance(n, ast.Expr))
    assert ConditionTest(loops=True).matches(statement, ctx6) is False
    literal, ctx7 = _node("def f():\n    if 1:\n        pass\n", lambda n: isinstance(n, ast.Constant))
    assert ConditionTest(loops=True).matches(literal, ctx7) is False


# --- Boolean operands ------------------------------------------------------------------------------------------


def test_each_boolean_operand_is_dropped() -> None:
    found = {m for _o, _r, m in _changes("def f(a, b, c):\n    return a and b and c\n", "lang:boolop:And-drop")}
    assert found == {"b and c", "a and c", "a and b"}
    two = {m for _o, _r, m in _changes("def f(a, b):\n    return a or b\n", "lang:boolop:Or-drop")}
    assert two == {"a", "b"}


def test_a_low_precedence_operand_left_alone_is_parenthesized() -> None:
    source = "def f(a, b, c, d):\n    return x if (b if c else d) or a else y\n"
    found = {m for _o, _r, m in _changes(source, "lang:boolop:Or-drop")}
    assert found == {"a", "(b if c else d)"}
    for text in _mutated(source, "lang:boolop:Or-drop"):
        assert ast.parse(text)
    lam = {m for _o, _r, m in _changes("def f(a):\n    return (lambda: 1) or a\n", "lang:boolop:Or-drop")}
    assert "(lambda: 1)" in lam
    walrus = {m for _o, _r, m in _changes("def f(a):\n    return (m := a) and m\n", "lang:boolop:And-drop")}
    assert "(m := a)" in walrus
    plain = {m for _o, _r, m in _changes("def f(a, b):\n    return a.x or b\n", "lang:boolop:Or-drop")}
    assert "a.x" in plain


# --- Arithmetic --------------------------------------------------------------------------------------------------


def test_a_term_of_two_non_literal_operands_is_dropped() -> None:
    found = _changes("def f(a, b):\n    return a - b\n", "lang:binop:Sub->")
    assert ("lang:binop:Sub->lhs", "a - b", "a") in found
    assert ("lang:binop:Sub->rhs", "a - b", "b") in found
    nested = {m for _o, _r, m in _changes("def f(a, b, c):\n    return a * (b + c)\n", "lang:binop:Mult->")}
    assert "(b + c)" in nested


def test_a_literal_operand_or_a_format_keeps_its_terms() -> None:
    assert not _changes("def f(a):\n    return a + 1\n", "lang:binop:Add->lhs")
    assert not _changes("def f(a):\n    return 2 * a\n", "lang:binop:Mult->rhs")
    assert not _changes("def f(a, b):\n    return '%s' % a\n", "lang:binop:Mod->lhs")
    assert not _changes("def f(a, b):\n    return f'{b}%s' % a\n", "lang:binop:Mod->rhs")
    assert not _changes("def f(a, b):\n    return a @ b\n", "lang:binop:MatMult")
    assert _changes("def f(a, b):\n    return a % b\n", "lang:binop:Mod->lhs")


def test_operand_operation_matcher() -> None:
    node, ctx = _node("x\n", lambda n: isinstance(n, ast.Name))
    assert OperandOperation().matches(node, ctx) is False
    fmt, ctx2 = _node("b'%s' % a\n", lambda n: isinstance(n, ast.BinOp))
    assert OperandOperation().matches(fmt, ctx2) is False
    mod, ctx3 = _node("a % b\n", lambda n: isinstance(n, ast.BinOp))
    assert OperandOperation().matches(mod, ctx3) is True
    add, ctx4 = _node("'a' + b\n", lambda n: isinstance(n, ast.BinOp))
    assert OperandOperation().matches(add, ctx4) is True


def test_true_division_swaps_to_floor_division() -> None:
    assert ("lang:binop:Div->FloorDiv", "a / b", "a // b") in _changes("def f(a, b):\n    return a / b\n", "lang:binop")


# --- Constants ---------------------------------------------------------------------------------------------------


def test_numbers_shift_both_ways() -> None:
    found = {m for _o, _r, m in _changes("def f(x):\n    return x[0] + 2.5\n", "lang:const:num")}
    assert found == {"1", "-1", "3.5", "1.5"}


def test_a_sign_is_dropped() -> None:
    cases = {
        "x[-1]": "x[1]",
        "-x": "x",
        "c * -(a + b)": "c * (a + b)",
        "a - - b": "a -  b",
        "-x ** 2": "x ** 2",
        "2 ** -x": "2 ** x",
    }
    for expression, expected in cases.items():
        texts = _mutated(f"def f(a, b, c, x):\n    return {expression}\n", "lang:unary:neg-removal")
        assert [text.splitlines()[1] for text in texts] == [f"    return {expected}"], expression
    assert ("lang:unary:neg-removal", "-", "") in _changes("def f(x):\n    return -x\n", "lang:unary")
    assert not _changes("def f(x):\n    return +x\n", "lang:unary:neg")
    assert not _changes("def f(x):\n    return not x\n", "lang:unary:neg")


def test_string_literals_are_wrapped_or_filled() -> None:
    source = 'def f(s):\n    return s.split(", ") + [""] + [b"ab", b""]\n'
    found = _changes(source, "lang:const")
    assert ("lang:const:str:wrap", '", "', "'XX, XX'") in found
    assert ("lang:const:str:fill", '""', "'XX'") in found
    assert ("lang:const:bytes:wrap", 'b"ab"', "b'XXabXX'") in found
    assert ("lang:const:bytes:fill", 'b""', "b'XX'") in found
    mutant = next(m for m in enumerate_mutants("t.py", source) if m.operator == "lang:const:str:wrap")
    assert mutant.bc is not None
    assert mutant.bc.mutated == "'XX, XX'"
    assert classify(mutant) is Bucket.REVIEW


def test_a_module_table_string_is_mutated_too() -> None:
    found = _changes('TABLE = {"now": "just nu"}\n', "lang:const:str")
    assert {m for _o, _r, m in found} == {"'XXnowXX'", "'XXjust nuXX'"}


def test_arid_strings_are_left_alone() -> None:
    source = '''
        """Module."""
        import logging
        import warnings
        from typing import Annotated, Literal, NewType, TypeVar, cast
        __all__ = ["f"]
        __slots__ = ("a",)
        log = logging.getLogger("pkg")
        T = TypeVar("T")
        UserId = NewType("UserId", int)
        Mode = Literal["r", "w"]
        Meta = Annotated[int, "meta"]
        def f(x):
            """Doc."""
            "a bare string"
            log.info("seen %s", x)
            self.logger.warning("w")
            warnings.warn("old")
            assert x, "message"
            y = cast("int", x)
            z = f"text {x!r:>10}"
            t = t"text {x}"
            if __name__ == "__main__":
                pass
            return y, z, t
    '''
    assert not _changes(source, "lang:const:str")


def test_strings_next_to_arid_ones_still_mutate() -> None:
    source = """
        def f(x, logger):
            __all__.append("g")
            result = {"k": ", ".join(x)}
            logger.info("seen")
            other.info("value")
            catalog.info("entry")
            dialog.error("shown")
            LOG.info("quiet")
            app_logger.debug("quiet")
            assert "literal"
            raise ValueError("bad input")
    """
    found = {r for _o, r, _m in _changes(source, "lang:const:str")}
    assert found == {'"g"', '"k"', '", "', '"value"', '"entry"', '"shown"', '"literal"', '"bad input"'}
    assert not _changes('x = __all__ = ["a"]\n', "lang:const:str")


def test_arid_string_matcher_stops_at_the_statement() -> None:
    node, ctx = _node('x = "a"\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(node, ctx) is False
    declared, ctx2 = _node('__all__ += ["a"]\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(declared, ctx2) is True
    annotated, ctx3 = _node('__all__: list[str] = ["a"]\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(annotated, ctx3) is True
    other, ctx4 = _node('names += ["a"]\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(other, ctx4) is False
    call, ctx5 = _node('print("a")\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(call, ctx5) is False
    plain_log, ctx6 = _node('info("a")\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(plain_log, ctx6) is False
    computed, ctx7 = _node('make().info("a")\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(computed, ctx7) is False
    subscript, ctx8 = _node('d["a"]\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(subscript, ctx8) is False
    compare, ctx9 = _node('x == "a"\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(compare, ctx9) is False
    expression, ctx10 = _node('f("a")\n', lambda n: isinstance(n, ast.Constant))
    assert AridString().matches(expression, ctx10) is False


def test_environment_probe_matcher() -> None:
    probe, ctx = _node("import os\nos.name == 'nt'\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(probe, ctx) is True
    debug, ctx2 = _node("x = __debug__\n", lambda n: isinstance(n, ast.Name) and n.id == "__debug__")
    assert EnvironmentProbe().matches(debug, ctx2) is True
    unrelated, ctx3 = _node("x = f(os.getcwd(), 'a')\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(unrelated, ctx3) is False
    call_arg, ctx4 = _node("f(sys.platform, 'a')\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(call_arg, ctx4) is False
    negated, ctx5 = _node("not (sys.maxsize > 2)\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(negated, ctx5) is True
    statement, ctx6 = _node("pass\n", lambda n: isinstance(n, ast.Pass))
    assert EnvironmentProbe().matches(statement, ctx6) is False
    computed, ctx7 = _node("x = f().platform == 'a'\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(computed, ctx7) is False
    branch, ctx8 = _node("if TYPE_CHECKING:\n    pass\n", lambda n: isinstance(n, ast.If))
    assert EnvironmentProbe().matches(branch, ctx8) is False
    conjunct, ctx9 = _node("TYPE_CHECKING and f(1)\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(conjunct, ctx9) is True
    negation, ctx10 = _node("not f(TYPE_CHECKING, 1)\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(negation, ctx10) is True
    plain, ctx11 = _node("-f(1)\n", lambda n: isinstance(n, ast.Constant))
    assert EnvironmentProbe().matches(plain, ctx11) is False


# --- State and control --------------------------------------------------------------------------------------------


def test_an_assigned_value_becomes_none() -> None:
    source = """
        def f(self, a):
            self.x = a
            y: int = g(a)
            self.d[a] = a + 1
            b = c = h()
            p, q = a
            r = 1
            s = -1
            t = []
            u = {}
            v = ()
            w = set()
            z = [a]
            return y
    """
    mutants = enumerate_mutants("t.py", textwrap.dedent(source))
    found = {(m.lineno, m.original) for m in mutants if m.operator == "lang:assign:None"}
    assert found == {(3, "a"), (4, "g(a)"), (5, "a + 1"), (6, "h()"), (13, "set()"), (14, "[a]")}
    more = """
        def f(a):
            [p, q] = a
            x = (m, n) = a
            y = -a
            z = not a
            w = (a,)
    """
    lines = {m.lineno for m in enumerate_mutants("t.py", textwrap.dedent(more)) if m.operator == "lang:assign:None"}
    assert lines == {5, 6, 7}
    for text in _mutated(source, "lang:assign:None"):
        assert "None" in text
    assert not _changes("x = compute()\n", "lang:assign")


def test_assigned_value_matcher_rejects_other_positions() -> None:
    node, ctx = _node("f(a)\n", lambda n: isinstance(n, ast.Name) and n.id == "a")
    assert AssignedValue().matches(node, ctx) is False
    target, ctx2 = _node("a = b\n", lambda n: isinstance(n, ast.Name) and n.id == "a")
    assert AssignedValue().matches(target, ctx2) is False
    annotation, ctx3 = _node("a: T = b\n", lambda n: isinstance(n, ast.Name) and n.id == "T")
    assert AssignedValue().matches(annotation, ctx3) is False
    full, ctx4 = _node("a = {1: 2}\n", lambda n: isinstance(n, ast.Dict))
    assert AssignedValue().matches(full, ctx4) is True


def test_a_handler_catches_nothing_or_everything() -> None:
    source = """
        def f():
            try:
                g()
            except ValueError:
                pass
            except (KeyError, IndexError) as error:
                raise
            except Exception:
                pass
            except (OSError, BaseException):
                pass
            except:
                pass
    """
    never = {orig for _op, orig, _m in _changes(source, "lang:except:never")}
    assert never == {"ValueError", "(KeyError, IndexError)", "Exception", "(OSError, BaseException)"}
    broad = {orig for _op, orig, _m in _changes(source, "lang:except:Exception")}
    assert broad == {"ValueError", "(KeyError, IndexError)"}
    assert all("except ()" in text or "except () as" in text for text in _mutated(source, "lang:except:never"))


def test_an_exception_group_handler_and_an_empty_tuple_are_left_alone() -> None:
    source = "def f():\n    try:\n        g()\n    except* ValueError:\n        pass\n"
    assert not _changes(source, "lang:except")
    assert not _changes("def f():\n    try:\n        g()\n    except ():\n        pass\n", "lang:except")
    node, ctx = _node("x\n", lambda n: isinstance(n, ast.Name))
    assert HandlerType(specific=False).matches(node, ctx) is False
    module = ast.parse("x\n")
    assert HandlerType(specific=False).matches(module, context("t.py", "x\n", tree=module)) is False
    body, ctx2 = _node("try:\n    g()\nexcept ValueError:\n    pass\n", lambda n: isinstance(n, ast.Pass))
    assert HandlerType(specific=False).matches(body, ctx2) is False
    group_source = "try:\n    g()\nexcept* ValueError:\n    pass\n"
    group, ctx3 = _node(group_source, lambda n: isinstance(n, ast.Name) and n.id == "ValueError")
    assert HandlerType(specific=False).matches(group, ctx3) is False


# --- Calls --------------------------------------------------------------------------------------------------------


def test_a_mapping_splat_is_dropped() -> None:
    found = {m for _o, _r, m in _changes("def f(a, kw):\n    return g(a, **kw)\n", "lang:call:kwargs-drop")}
    assert found == {""}
    texts = {
        text.splitlines()[1].strip()
        for source in (
            "def f(a, kw):\n    return g(a, **kw)\n",
            "def f(kw):\n    return g(**kw)\n",
            "def f(kw):\n    return g(**kw, b=1)\n",
            "def f(kw, o):\n    return g(**kw, **o)\n",
            "def f(kw):\n    return g(a=1, **kw)\n",
        )
        for text in _mutated(source, "lang:call:kwargs-drop")
    }
    assert texts == {"return g(a)", "return g()", "return g(b=1)", "return g(**o)", "return g(**kw)", "return g(a=1)"}
    assert not _changes("def f(a):\n    return g(a, b=1)\n", "lang:call:kwargs-drop")
    both = _mutated("def f(a, kw, o):\n    return g(a, **kw, **o)\n", "lang:call:kwargs-drop")
    assert sorted(text.splitlines()[1].strip() for text in both) == ["return g(a, **kw)", "return g(a, **o)"]
    # Across lines no comma sits right before the splat: the splat is left alone rather than spliced unsafely.
    assert not _changes("def f(a, kw):\n    return g(\n        a,\n        **kw,\n    )\n", "lang:call:kwargs-drop")


def test_a_splat_without_positions_is_skipped() -> None:
    source = "g(a, **kw, **o)\n"
    call, ctx = _node(source, lambda n: isinstance(n, ast.Call))
    assert isinstance(call, ast.Call)
    call.keywords[0].value.end_lineno = None
    found = list(DropEachMappingSplat().find(call, ctx))
    assert [(target, text) for target, text, _span, _op, _bc in found] == [(call.keywords[1].value, "")]


def test_conversions_and_normalizers_are_dropped() -> None:
    source = """
        def f(s, xs):
            a = int(s.strip())
            b = sorted(set(xs))
            s.lower()
            return str(a).upper(), abs(a), round(a), reversed(xs), frozenset(xs), float(a)
    """
    drops = {(op, m) for op, _o, m in _changes(source, "std:")}
    assert ("std:builtins:int-drop", "s.strip()") in drops
    assert ("std:str:strip-drop", "s") in drops
    assert ("std:builtins:sorted-drop", "set(xs)") in drops
    assert ("std:builtins:set-drop", "xs") in drops
    assert ("std:str:upper-drop", "str(a)") in drops
    assert ("std:builtins:str-drop", "a") in drops
    for name in ("abs", "round", "reversed", "frozenset", "float"):
        assert any(op == f"std:builtins:{name}-drop" for op, _m in drops), name
    assert not any(op == "std:str:lower-drop" for op, _m in drops)  # a bare statement: the result is unused


def test_value_used_matcher() -> None:
    bare, ctx = _node("s.lower()\n", lambda n: isinstance(n, ast.Call))
    assert ValueUsed().matches(bare, ctx) is False
    used, ctx2 = _node("x = s.lower()\n", lambda n: isinstance(n, ast.Call))
    assert ValueUsed().matches(used, ctx2) is True


# --- Defaults ------------------------------------------------------------------------------------------------------


def test_every_literal_default_is_mutated_positional_and_keyword_only() -> None:
    source = "def f(a, n=1, s='x', *, flag=False, sep='', none=None, call=g()):\n    return a\n"
    found = {(m.operator, m.mutated, m.default_index, m.default_name) for m in enumerate_defaults("t.py", source)}
    assert found == {
        ("lang:const:num:1->2", "2", 0, None),
        ("lang:const:num:1->0", "0", 0, None),
        ("lang:const:str:wrap", "'XXxXX'", 1, None),
        ("lang:const:bool:False->True", "True", None, "flag"),
        ("lang:const:str:fill", "'XX'", None, "sep"),
    }
    mutants = enumerate_defaults("t.py", source)
    assert all(m.lineno == 2 and m.site_lineno == 1 for m in mutants)
    assert all(mutant_mechanism(m) is ApplyMechanism.DEFAULTS for m in mutants)


def test_a_nested_function_keyword_default_is_not_reachable() -> None:
    source = "def outer():\n    def inner(*, flag=False):\n        return flag\n    return inner\n"
    assert enumerate_defaults("t.py", source) == []
    branch = "def outer(x):\n    if x:\n        def inner(n=1, *, flag=False):\n            return flag\n    return x\n"
    assert enumerate_defaults("t.py", branch) == []
    awaited = "async def outer():\n    def inner(n=1, *, flag=False):\n        return flag\n    return inner\n"
    assert enumerate_defaults("t.py", awaited) == []
    method = "class C:\n    def m(self, *, flag=False):\n        return flag\n"
    assert [m.default_name for m in enumerate_defaults("t.py", method)] == ["flag"]


def test_the_campaign_kind_carries_the_defaults_phase() -> None:
    # Campaigns enumerate the registered kind: its mutants are those of ``enumerate_mutants``, defaults included.
    source = "def f(x=1, *, flag=False):\n    return x + 1\n"
    kind = resolve_kinds(["src"], KindOverrides())["src"]
    campaign = enumerate_facts("t.py", source, (kind,), family="src")
    assert [(m.operator, m.mutated) for m in campaign] == [
        (m.operator, m.mutated) for m in enumerate_mutants("t.py", source)
    ]
    assert any(m.default_index == 0 for m in campaign)
    assert any(m.default_name == "flag" for m in campaign)


# --- Finders and predicates on their own --------------------------------------------------------------------------


def test_every_new_finder_ignores_a_node_of_another_shape() -> None:
    source = "x\nnot x\nx += 1\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    negation = next(n for n in ast.walk(tree) if isinstance(n, ast.UnaryOp))
    number = next(n for n in ast.walk(tree) if isinstance(n, ast.Constant))
    for finder in (
        DropEachMappingSplat(),
        KeepOperandSite(),
        DropBoolOperandSite(),
        StringLiteralSite(),
        SignDropSite(),
    ):
        assert list(finder.find(name, ctx)) == [], finder
    assert list(SignDropSite().find(negation, ctx)) == []
    assert list(StringLiteralSite().find(number, ctx)) == []


def test_operand_operation_formats_only_strings() -> None:
    number, ctx = _node("5 % a\n", lambda n: isinstance(n, ast.BinOp))
    assert OperandOperation().matches(number, ctx) is True
    text, ctx2 = _node("'%s' % a\n", lambda n: isinstance(n, ast.BinOp))
    assert OperandOperation().matches(text, ctx2) is False


def test_the_arid_predicates_answer_false_not_just_falsy() -> None:
    plain = ast.parse("f(x)", mode="eval").body
    assert isinstance(plain, ast.Call)
    assert _arid_call(plain) is False
    assert _arid_expression(plain) is False
    assert _arid_expression(ast.parse("x", mode="eval").body) is False
    assert _arid_expression(ast.parse("d[1]", mode="eval").body) is False
    assert _probes_environment(ast.parse("x + 1")) is False
    assert _declares(ast.parse("x = 1").body[0]) is False
    assert _declares(ast.parse("pass").body[0]) is False
