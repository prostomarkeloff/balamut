"""Adapter logic that runs without the tools: irradiate's splice of a function variant into the original module."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pymutbench" / "adapters"))

from ad_irradiate import _origin, _physical_lines, _target, _targets, splice

TABS = (
    "def add(a, b):\n\tif a > b:\n\t\treturn a - b\n\treturn a + b\n\n\nclass Box:\n\tdef scaled(self, factor):\n"
    "\t\ttotal = self.value * factor\n\t\treturn total\n"
)
SPACES = TABS.replace("\t", "    ")
OVERLOADED = (
    "import typing as t\n\n\nclass Context:\n    @t.overload\n    def invoke(self, callback: int) -> int: ...\n\n"
    "    @t.overload\n    def invoke(self, callback: str) -> str: ...\n\n"
    "    def invoke(self, callback):\n        value = callback\n        if value is None:\n            return 0\n"
    "        return value\n"
)
ACCESSORS = (
    "class Box:\n    @property\n    def size(self):\n        return self._size\n\n"
    "    @size.setter\n    def size(self, value):\n        if value < 0:\n            raise ValueError(value)\n"
    "        self._size = value\n"
)


def variant_text(source: str, cls: str | None, name: str, old: str, new: str) -> str:
    """A variant as irradiate writes it: the function at module level, its body one level in."""
    node = _target(ast.parse(source), cls, name)
    assert node is not None
    return ast.unparse(node).replace(old, new, 1)


class SpliceTest(unittest.TestCase):
    def check(self, source: str, cls: str | None, name: str, old: str, new: str) -> None:
        lines = _physical_lines(source)
        target = _target(ast.parse(source), cls, name)
        assert target is not None
        spliced = splice(lines, target, 0, variant_text(source, cls, name, old, new))
        mutated = _target(ast.parse(spliced), cls, name)
        assert mutated is not None
        expected = ast.parse(variant_text(source, cls, name, old, new)).body[0]
        self.assertEqual(ast.dump(mutated, include_attributes=False), ast.dump(expected, include_attributes=False))
        # Every other statement of the module is kept as it was.
        others = [ast.dump(n) for n in ast.parse(spliced).body if getattr(n, "name", None) not in {name, cls}]
        self.assertEqual(
            others, [ast.dump(n) for n in ast.parse(source).body if getattr(n, "name", None) not in {name, cls}]
        )

    def test_method_of_a_tab_indented_class(self) -> None:
        self.check(TABS, "Box", "scaled", "total = self.value * factor", "total = self.value / factor")

    def test_module_function_in_a_tab_indented_file(self) -> None:
        self.check(TABS, None, "add", "a + b", "a - b")

    def test_method_of_a_space_indented_class(self) -> None:
        self.check(SPACES, "Box", "scaled", "total = self.value * factor", "total = self.value / factor")

    def test_the_def_line_keeps_the_original_indentation_characters(self) -> None:
        lines = _physical_lines(TABS)
        target = _target(ast.parse(TABS), "Box", "scaled")
        assert target is not None
        spliced = splice(lines, target, 0, variant_text(TABS, "Box", "scaled", "* factor", "/ factor"))
        self.assertIn("\n\tdef scaled(self, factor):\n", spliced)


class OriginTest(unittest.TestCase):
    """A body that defines a name more than once: the variant goes into the definition it was generated from."""

    def spliced(self, source: str, index: int, old: str, new: str) -> tuple[ast.Module, int]:
        candidates = _targets(ast.parse(source), "Context" if "Context" in source else "Box",
                              "invoke" if "invoke" in source else "size")
        variant = ast.parse(ast.unparse(candidates[index]).replace(old, new, 1)).body[0]
        variant.decorator_list = []  # irradiate writes a variant as a plain function; the splice keeps the decorators
        chosen = _origin(candidates, variant, variant.name)
        text = ast.unparse(variant)
        out = splice(_physical_lines(source), candidates[chosen], 0, text)
        return ast.parse(out), chosen

    def test_the_implementation_after_overload_stubs(self) -> None:
        tree, chosen = self.spliced(OVERLOADED, 2, "return 0", "return 1")
        self.assertEqual(chosen, 2)
        defs = _targets(tree, "Context", "invoke")
        self.assertEqual([ast.unparse(d) for d in defs[:2]],
                         [ast.unparse(d) for d in _targets(ast.parse(OVERLOADED), "Context", "invoke")[:2]])
        self.assertIn("return 1", ast.unparse(defs[2]))

    def test_a_property_setter_after_its_getter(self) -> None:
        tree, chosen = self.spliced(ACCESSORS, 1, "value < 0", "value <= 0")
        self.assertEqual(chosen, 1)
        getter, setter = _targets(tree, "Box", "size")
        self.assertEqual(ast.unparse(getter), ast.unparse(_targets(ast.parse(ACCESSORS), "Box", "size")[0]))
        self.assertIn("value <= 0", ast.unparse(setter))

    def test_a_property_getter_keeps_its_place(self) -> None:
        tree, chosen = self.spliced(ACCESSORS, 0, "return self._size", "return None")
        self.assertEqual(chosen, 0)
        self.assertIn("return None", ast.unparse(_targets(tree, "Box", "size")[0]))

    def test_the_name_is_bound_to_the_last_definition(self) -> None:
        last = _target(ast.parse(OVERLOADED), "Context", "invoke")
        assert last is not None
        self.assertFalse(last.decorator_list)


if __name__ == "__main__":
    unittest.main()
