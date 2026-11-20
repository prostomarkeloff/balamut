"""The src operators that undo what a single-statement fix adds most often: the last element of a display, the
right name in place of a similar one, and the neighbouring library method.

Each test pins both what an operator emits and what it must leave alone; every mutated module must parse."""

from __future__ import annotations

import ast
import textwrap

from balamut.core.mutants import apply_mutant
from balamut.operators.kit import DropLastElementSite, context
from balamut.operators.src import (
    _SCOPE_KEY,
    DataDisplay,
    ScopeFacts,
    SimilarNameSite,
    _alike,
    _closest,
    _words,
    enumerate_mutants,
)


def _mutations(source: str, operator: str) -> list[str]:
    """The mutated texts of the lines one operator changes, each mutated module parsed."""
    source = textwrap.dedent(source)
    found: list[str] = []
    for mutant in enumerate_mutants("t.py", source):
        if mutant.operator != operator:
            continue
        mutated = apply_mutant(source, mutant)
        ast.parse(mutated)
        before, after = source.splitlines(), mutated.splitlines()
        if len(before) == len(after):
            changed = [line.strip() for line, old in zip(after, before, strict=True) if line != old]
        else:
            changed = [line.strip() for line in after if line not in before]
        found.append("\n".join(changed) or "<removed>")
    return found


# --- Display elements ------------------------------------------------------------------------------------------


def test_the_last_element_of_a_display_is_dropped() -> None:
    source = """
    def f(a, b, c, d):
        xs = [a, b, c]
        ys = {a, b}
        zs = {"a": a, "b": b, "c": c}
        ws = {**d, "c": c}
        vs = [a, g(b)]
        us = [a, b,]
        ts = (a, b, c)
        return isinstance(a, (int, str)), b in (1, 2,)

    def g(a, b):
        return [a, b]
    """
    assert _mutations(source, "lang:container:drop-last") == [
        "xs = [a, b]",
        "ys = {a}",
        'zs = {"a": a, "b": b}',
        "ws = {**d}",
        "vs = [a]",
        "us = [a,]",
        "ts = (a, b)",
        "return [a]",  # a returned list is data
        "return isinstance(a, (int,)), b in (1, 2,)",
        "return isinstance(a, (int, str)), b in (1,)",
    ]


def test_a_multiline_display_keeps_its_layout_and_trailing_comma() -> None:
    source = textwrap.dedent("""
    def f(a, b):
        xs = [
            a,
            (b),
        ]
        return xs
    """)
    [mutant] = [m for m in enumerate_mutants("t.py", source) if m.operator == "lang:container:drop-last"]
    assert apply_mutant(source, mutant) == textwrap.dedent("""
    def f(a, b):
        xs = [
            a,
        ]
        return xs
    """)


def test_the_cut_skips_tabs_spaces_and_crlf_line_ends_before_the_bracket() -> None:
    source = (
        "def f(a, b):\r\n    xs = [\r\n        a,\r\n        b\r\n    ]\r\n    ys = [a, b ]\r\n    zs = [a, b\t]\r\n"
    )
    mutated = [
        apply_mutant(source, m) for m in enumerate_mutants("t.py", source) if m.operator == "lang:container:drop-last"
    ]
    assert mutated == [
        source.replace("a,\r\n        b\r\n", "a\r\n"),
        source.replace("[a, b ]", "[a ]"),
        source.replace("[a, b\t]", "[a\t]"),
    ]


def test_displays_that_are_no_data_are_left_alone() -> None:
    source = """
    import logging
    log = logging.getLogger(__name__)

    def f(a, b):
        [x, y] = a
        first, second = (a, b)
        [p, q] = (a, b)
        r = (s, t) = (a, b)
        for item in a, b:
            pass
        for (k, v) in a:
            pass
        log.info("%s", [a, b])
        single = [a]
        empty = ()
        yield (a, b)
        return (a, b)
    """
    assert _mutations(source, "lang:container:drop-last") == []


def test_data_display_matcher() -> None:
    source = "x = [a, b]\n(c, d) = e\nf((g, h))\ndef r():\n    return (i, j)\n    yield (k, l)\nm, n = (o, p)\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    matched = {
        ast.unparse(n): DataDisplay().matches(n, ctx) for n in ast.walk(tree) if isinstance(n, ast.List | ast.Tuple)
    }
    assert matched == {
        "[a, b]": True,
        "(g, h)": True,
        "(c, d)": False,
        "(i, j)": False,
        "(k, l)": False,
        "(m, n)": False,
        "(o, p)": False,
    }
    name = next(n for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert DataDisplay().matches(name, ctx) is False


def test_drop_last_element_skips_what_it_cannot_shorten() -> None:
    source = "f(a, b)\n[a]\n{a: 1}\nx = a, b\ny = a, b, cd\nd[a, g(b)]\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    single = next(n for n in ast.walk(tree) if isinstance(n, ast.List))
    one_entry = next(n for n in ast.walk(tree) if isinstance(n, ast.Dict))
    bare = [n for n in ast.walk(tree) if isinstance(n, ast.Tuple)]  # without brackets of their own
    for node in (call, single, one_entry, *bare):
        assert list(DropLastElementSite().find(node, ctx)) == [], ast.dump(node)


def test_drop_last_element_needs_the_positions_of_the_display_and_its_elements() -> None:
    source = "[a, b]\n[c, d]\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    unplaced, unplaced_element = [n for n in ast.walk(tree) if isinstance(n, ast.List)]
    unplaced.end_lineno = None
    unplaced_element.elts[0].end_lineno = None
    for node in (unplaced, unplaced_element):
        assert list(DropLastElementSite().find(node, ctx)) == []


# --- Similar names -------------------------------------------------------------------------------------------------


def test_a_variable_becomes_a_similar_one_in_scope() -> None:
    source = """
    def f(items, start_x, end_x, val1, val2):
        item = items[0]
        width = end_x - start_x
        return item, width, val1
    """
    assert _mutations(source, "lang:name:similar") == [
        "width = start_x - start_x",
        "width = end_x - end_x",
        "return item, width, val2",
    ]


def test_names_outside_the_function_scope_are_left_alone() -> None:
    source = """
    LIMIT_LOW = 1
    LIMIT_HIGH = 2

    def handle(first_value, last_value, make_one, make_two):
        def inner():
            return last_value
        total = [first_value for first_value in last_value]
        handler = make_one(first_value)
        return LIMIT_LOW, inner, total, handler, make_two
    """
    assert sorted(_mutations(source, "lang:name:similar")) == [
        "handler = make_one(last_value)",  # a callee keeps its name
        "return LIMIT_LOW, inner, total, handler, make_one",  # module names are not the function's
        "return first_value",  # an enclosing function's names are in scope of a nested one
        "total = [first_value for first_value in first_value]",
        "total = [last_value for first_value in last_value]",
    ]


def test_the_functions_of_classes_and_blocks_are_scopes_too() -> None:
    source = """
    if True:
        def f(start_x, end_x):
            return start_x

    class C:
        def m(self, start_y, end_y):
            return start_y
    """
    assert sorted(_mutations(source, "lang:name:similar")) == ["return end_x", "return end_y"]


def test_a_function_binds_its_parameters_its_stores_and_its_handler_names() -> None:
    source = """
    def f(head_opts, **tail_opts):
        start_x = 0
        end_x = 1
        try:
            pass
        except ValueError as first_error:
            last_error = None
        return head_opts, start_x, last_error
    """
    assert sorted(_mutations(source, "lang:name:similar")) == [
        "return head_opts, end_x, last_error",
        "return head_opts, start_x, first_error",
        "return tail_opts, start_x, last_error",
    ]


def test_nested_scopes_comprehensions_annotations_and_globals() -> None:
    source = """
    LIMIT_LOW = 1

    def outer(first_value, limit_high, kind_a, kind_b: kind_a):
        def inner():
            last_value = first_value
            return last_value

        last_item = None
        picked = [first_item for first_item in limit_high if kind_a]

        async def later():
            last_value = 1
            return first_value

        return LIMIT_LOW, last_item, picked, inner, later
    """
    assert sorted(_mutations(source, "lang:name:similar")) == [
        "last_value = last_value",  # an enclosing function's names are in scope of a nested one
        "picked = [first_item for first_item in limit_high if kind_b]",  # a comprehension reads the function's names
        "return first_value",
        "return last_value",
    ]  # the annotation, the module name and the names only nested scopes bind are left alone


def test_a_method_of_self_becomes_a_similar_member() -> None:
    source = """
    class Box:
        def __init__(self, width):
            self.min_width = width
            self.max_width = width

        def parse_a(self):
            return self.min_width

        def parse_b(self):
            return self.parse_a()
    """
    assert _mutations(source, "lang:method:similar") == ["return self.parse_b()"]
    found = {m.operator for m in enumerate_mutants("t.py", textwrap.dedent(source))}
    assert not {op for op in found if op.startswith("lang:attr")}  # an attribute read keeps its name


def test_the_methods_of_a_class_are_those_it_defines_and_calls_on_its_receiver() -> None:
    source = """
    class Box(Base):
        @staticmethod
        def make():
            return 1

        def run_task_a(self):
            self.close()
            return self.run_task_b()

        async def check_item_a(self):
            return self.check_item_b()

        def check_item_b(self, other, zed):
            def inner(value=self.check_item_a()):
                return value
            return self.run_task_a(), run(self.check_item_a), other.run_task_a(), zed.run_task_a(), inner
    """
    assert sorted(_mutations(source, "lang:method:similar")) == [
        "return self.check_item_a()",
        "return self.run_task_a()",
        # an inherited method called on the receiver is a method of the class
        "return self.run_task_b(), run(self.check_item_a), other.run_task_a(), zed.run_task_a(), inner",
    ]  # a default, a method passed on, one of another receiver and one without a similar method keep their names


def test_a_method_without_a_located_name_keeps_it() -> None:
    source = "class C:\n    def a_x(self):\n        return self.a_y()\n"
    tree = ast.parse(source)
    ctx = context("t.py", source, tree=tree)
    called = next(n for n in ast.walk(tree) if isinstance(n, ast.Attribute))
    ctx.remember(_SCOPE_KEY, ScopeFacts({id(called): "a_x"}))
    assert [text for _n, text, *_rest in SimilarNameSite().find(called, ctx)] == ["a_x"]
    called.end_lineno = None
    assert list(SimilarNameSite().find(called, ctx)) == []


def test_alike_names_are_one_word_apart() -> None:
    assert _alike("val1", "val2")
    assert _alike("start_line", "end_line")
    assert _alike("handleNode", "handleAnnotation")
    assert not _alike("item", "items")  # one word: a collection and its element
    assert not _alike("x", "y")
    assert not _alike("name", "name")
    assert not _alike("__init_x__", "__init_y__")
    assert not _alike("start_line", "end_column")
    assert not _alike("items", "itemz_x")
    assert _alike("start_line_no", "end_line_no")
    assert not _alike("start_line", "startLine")  # the same words
    assert not _alike("__private_x", "private_y")
    assert not _alike("private_x", "__private_y")
    assert _closest("val1", {"val9", "val2", "item"}) == "val2"
    assert _closest("item", {"items"}) is None


def test_words() -> None:
    assert _words("startLine_x2") == ("start", "line", "x", "2")
    assert _words("HTTPServer") == ("http", "server")


# --- Similar library methods ---------------------------------------------------------------------------------------


def test_a_library_method_becomes_its_neighbour() -> None:
    source = """
    import math
    import re

    def f(xs, ys, y, s):
        xs.extend(y)
        ys.append(y)  # the commonest of these calls is left alone
        m = re.match(s, y)
        return math.floor(y), m
    """
    found = {
        m.operator for m in enumerate_mutants("t.py", textwrap.dedent(source)) if m.operator.startswith("std:method:")
    }
    assert found == {"std:method:extend->append", "std:method:match->search", "std:method:floor->ceil"}
