"""Where a fix changed a file, witness classes, transplants into the buggy file and scope noise."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymutbench import fixlines

BUGGY = """import sys
__version__ = '1'


def g(x):
    '''Doc one.'''
    return x + 1


def h(x):
    return x * 2


def k(items):
    total = 0
    for item in items:
        total += item
    return total
"""
FIXED = """import os
__version__ = '2'


def g(x):
    '''Doc two.'''
    # a comment
    return x + 1


def h(x):
    return x * 3


def k(items):
    total = 0
    for item in items:
        if item is None:
            continue
        total += item
    return total
"""


def diff_u0(old: str, new: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "a.py"), Path(tmp, "b.py")
        a.write_text(old, encoding="utf-8")
        b.write_text(new, encoding="utf-8")
        return subprocess.run(
            ["git", "diff", "--no-index", "-U0", str(a), str(b)], capture_output=True, text=True, check=False
        ).stdout


class FixLinesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.diff = diff_u0(BUGGY, FIXED)
        self.hunks = fixlines.hunks(self.diff)
        self.fixed = FIXED.encode()

    def edit(self, old: str, new: str, occurrence: int = 0) -> tuple[int, int, str]:
        start = -1
        for _ in range(occurrence + 1):
            start = self.fixed.index(old.encode(), start + 1)
        return start, start + len(old.encode()), new

    def test_inserted_and_rewritten_lines(self) -> None:
        inserted, rewritten = fixlines.fix_lines(self.diff)
        self.assertEqual(inserted, {18, 19})
        self.assertEqual(rewritten, {1, 2, 6, 7, 12})
        self.assertEqual(fixlines.composition([self.diff]), "mixed")

    def test_classes(self) -> None:
        inserted, rewritten = fixlines.fix_lines(self.diff)
        for old, new, expected in (
            ("x * 3", "x / 3", "rewritten"),
            ("is None", "is not None", "inserted"),
            ("total += item", "total -= item", "unchanged"),
        ):
            start, end, replacement = self.edit(old, new)
            touched = fixlines.touched_lines(self.fixed, start, end, replacement)
            self.assertEqual(fixlines.classify(touched, inserted, rewritten), expected, old)
        self.assertEqual(fixlines.best({"inserted", "rewritten"}), "rewritten")

    def test_touched_lines_ignore_a_reindented_body(self) -> None:
        start = self.fixed.index(b"def k")
        end = self.fixed.index(b"return total") + len(b"return total")
        body = self.fixed[start:end].decode().replace("\n    ", "\n        ").replace("total += item", "total -= item")
        self.assertEqual(fixlines.touched_lines(self.fixed, start, end, body), {20})

    def test_parent_lines(self) -> None:
        self.assertEqual(fixlines.to_parent_line(20, self.hunks), 17)
        self.assertIsNone(fixlines.to_parent_line(18, self.hunks))
        self.assertEqual(fixlines.to_parent_line(3, self.hunks), 3)

    def test_transplant_of_untouched_code(self) -> None:
        start, end, replacement = self.edit("total += item", "total -= item")
        mutated = fixlines.transplant(self.fixed, start, end, replacement, BUGGY.encode(), self.hunks)
        self.assertEqual(mutated, BUGGY.replace("total += item", "total -= item").encode())

    def test_transplant_of_a_reindented_variant_keeps_the_buggy_indentation(self) -> None:
        start = self.fixed.index(b"def k")
        end = self.fixed.index(b"return total") + len(b"return total")
        body = self.fixed[start:end].decode().replace("\n    ", "\n        ").replace("total += item", "total -= item")
        mutated = fixlines.transplant(self.fixed, start, end, body, BUGGY.encode(), self.hunks)
        self.assertEqual(mutated, BUGGY.replace("total += item", "total -= item").encode())

    def test_transplant_of_an_inserted_line_keeps_its_own_indentation(self) -> None:
        # Before a blank line, and before a line of the enclosing block: the line below gives no indentation.
        for anchor, line, buggy_anchor in (
            ("    return x + 1\n", "    return 0\n", "    return x + 1\n"),
            ("        total += item\n", "        break\n", "        total += item\n"),
        ):
            start = self.fixed.index(anchor.encode()) + len(anchor)
            mutated = fixlines.transplant(self.fixed, start, start, line, BUGGY.encode(), self.hunks)
            self.assertEqual(mutated, BUGGY.replace(buggy_anchor, buggy_anchor + line).encode(), line)

    def test_transplant_of_an_inserted_line_in_a_reindented_variant(self) -> None:
        start = self.fixed.index(b"def k")
        end = self.fixed.index(b"return total") + len(b"return total")
        body = self.fixed[start:end].decode().replace("\n    ", "\n        ")
        body = body.replace("total += item\n", "total += item\n            break\n")
        mutated = fixlines.transplant(self.fixed, start, end, body, BUGGY.encode(), self.hunks)
        expected = BUGGY.replace("        total += item\n", "        total += item\n        break\n")
        self.assertEqual(mutated, expected.encode())

    def test_no_transplant_on_lines_the_fix_changed(self) -> None:
        for old, new in (("x * 3", "x / 3"), ("is None", "is not None")):
            start, end, replacement = self.edit(old, new)
            self.assertIsNone(fixlines.transplant(self.fixed, start, end, replacement, BUGGY.encode(), self.hunks))

    def test_noise(self) -> None:
        reasons = [
            fixlines.noise(FIXED, BUGGY, self.diff, start, end) for start, end in ((1, 1), (2, 2), (5, 8), (11, 12))
        ]
        self.assertEqual(reasons, ["import", "metadata", "no code change", None])


if __name__ == "__main__":
    unittest.main()
