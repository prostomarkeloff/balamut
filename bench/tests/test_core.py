"""The scope of a fix, the statistics, normalized artifacts and AST materialization."""

from __future__ import annotations

import ast
import itertools
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymutbench import astmat, scope, stats
from pymutbench.artifacts import compiled_key, edit_lines, minimal_edit

SOURCE = """import os

LIMIT = 3


def plain(x):
    return x + 1


@decorator
def decorated(x):
    def inner(y):
        return y * 2
    return inner(x)


class Box:
    size = 4

    def grow(self):
        return self.size + 1
"""


class ScopeTest(unittest.TestCase):
    def test_changed_lines_of_hunks_and_deletions(self) -> None:
        diff = "@@ -3,0 +4,2 @@\n+a\n+b\n@@ -9 +10 @@\n-c\n+d\n@@ -20,2 +20,0 @@\n-e\n-f\n"
        self.assertEqual(scope.changed_lines(diff), {4, 5, 10, 20, 21})

    def test_regions(self) -> None:
        found = {(r.kind, r.name) for r in scope.regions(SOURCE, {3, 7, 13, 18, 21})}
        self.assertEqual(
            found,
            {
                ("module-statement", "Assign"),
                ("function", "plain"),
                ("function", "inner"),
                ("class-statement", "Box.Assign"),
                ("function", "grow"),
            },
        )

    def test_a_decorator_belongs_to_its_function(self) -> None:
        region = scope.regions(SOURCE, {10})[0]
        self.assertEqual((region.start, region.end, region.name), (10, 14, "decorated"))

    def test_nested_regions_merge_into_the_outer_one(self) -> None:
        self.assertEqual([r.name for r in scope.regions(SOURCE, {11, 13})], ["decorated"])

    def test_in_scope_needs_the_whole_edit_inside(self) -> None:
        regions = scope.regions(SOURCE, {7})
        self.assertTrue(scope.in_scope(regions, 6, 7))
        self.assertFalse(scope.in_scope(regions, 7, 10))


class StatsTest(unittest.TestCase):
    def test_hit_probability_against_enumeration(self) -> None:
        for population, hits, draws in ((5, 1, 2), (6, 2, 3), (7, 3, 1), (4, 4, 2), (5, 0, 3), (3, 1, 5)):
            items = [i < hits for i in range(population)]
            k = min(draws, population)
            samples = list(itertools.combinations(items, k))
            exact = sum(1 for s in samples if any(s)) / len(samples) if samples else 0.0
            self.assertAlmostEqual(stats.hit_probability(population, hits, draws), exact, places=12)

    def test_holm(self) -> None:
        self.assertEqual(stats.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])

    def test_bootstrap_is_seeded_and_matches_the_cluster_bootstrap(self) -> None:
        clusters = {"a": [1.0, 0.0, 1.0], "b": [0.0, 0.0], "c": [1.0, 1.0, 1.0, 0.0], "d": [0.0]}
        boot = stats.Bootstrap(list(clusters), replicates=2000)
        self.assertEqual(
            boot.percentile(clusters), stats.cluster_bootstrap(clusters, stats.pooled_mean, replicates=2000)
        )
        self.assertEqual(
            boot.replicates(clusters), stats.Bootstrap(list(clusters), replicates=2000).replicates(clusters)
        )
        low, high = boot.bca(clusters)
        self.assertLessEqual(low, stats.pooled_mean(clusters))
        self.assertGreaterEqual(high, stats.pooled_mean(clusters))

    def test_ranks_and_paired_difference(self) -> None:
        good = {"a": [1.0] * 5, "b": [1.0] * 5, "c": [1.0] * 5}
        bad = {"a": [0.0] * 5, "b": [0.0] * 5, "c": [0.0] * 5}
        boot = stats.Bootstrap(["a", "b", "c"], replicates=500)
        series = {"good": boot.replicates(good), "bad": boot.replicates(bad)}
        self.assertEqual(stats.Bootstrap.ranks(series), {"bad": (2, 2), "good": (1, 1)})
        result = stats.Bootstrap.difference(series["good"], series["bad"], 1.0)
        self.assertEqual(result["ci"], (1.0, 1.0))
        self.assertEqual(result["p"], 1 / 500)

    def test_spearman(self) -> None:
        self.assertAlmostEqual(stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertTrue(math.isnan(stats.spearman([1, 1, 1], [1, 2, 3])))


class ArtifactsTest(unittest.TestCase):
    def test_minimal_edit_never_splits_a_utf8_character(self) -> None:
        original, mutated = "x = 'é'\n".encode(), "x = 'è'\n".encode()
        edit = minimal_edit(original, mutated)
        self.assertEqual(original[: edit.start] + edit.replacement.encode() + original[edit.end :], mutated)
        self.assertEqual(edit.replacement, "è")

    def test_edit_lines(self) -> None:
        original = b"a = 1\nb = 2\nc = 3\n"
        self.assertEqual(edit_lines(original, minimal_edit(original, b"a = 1\nb = 5\nc = 3\n")), (2, 2))

    def test_compiled_key_ignores_layout_and_sees_semantics(self) -> None:
        self.assertEqual(compiled_key("x = (1 +\n 2)\n", "m.py"), compiled_key("x = 1 + 2\n", "m.py"))
        self.assertNotEqual(compiled_key("x = 1 + 2\n", "m.py"), compiled_key("x = 1 - 2\n", "m.py"))
        self.assertIsNone(compiled_key("x = (\n", "m.py"))


class AstMaterializeTest(unittest.TestCase):
    def check(self, source: str, mutate: ast.NodeTransformer) -> str:
        tree = mutate.visit(ast.parse(source))
        ast.fix_missing_locations(tree)
        text, reason = astmat.materialize(source, tree)
        self.assertIsNotNone(text, reason)
        assert text is not None
        return text

    def test_negative_constant(self) -> None:
        class Negate(ast.NodeTransformer):
            def visit_Constant(self, node: ast.Constant) -> ast.AST:
                return ast.Constant(-1) if node.value == 1 else node

        self.assertIn("-1", self.check("def f(x):\n    return x + 1\n", Negate()))

    def test_decorated_function_and_multi_line_statement(self) -> None:
        class Flip(ast.NodeTransformer):
            def visit_Add(self, node: ast.Add) -> ast.AST:  # noqa: ARG002 - the visitor protocol
                return ast.Sub()

        source = "@deco\ndef f(a, b):\n    return (a +\n            b)\n"
        text = self.check(source, Flip())
        self.assertTrue(text.startswith("@deco\ndef f(a, b):\n"))
        self.assertEqual(ast.dump(ast.parse(text)), ast.dump(Flip().visit(ast.parse(source))))


if __name__ == "__main__":
    unittest.main()
