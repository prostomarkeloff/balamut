"""The measures of one bug, on a synthetic bug directory: witnesses, their classes, the third condition."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymutbench import metrics
from pymutbench.artifacts import from_mutated

BUGGY = "def price(qty, rate):\n    total = qty * rate\n    return total\n"
FIXED = "def price(qty, rate):\n    total = qty * rate\n    if qty < 0:\n        return 0\n    return total\n"
PATH = "pkg/price.py"


def write(directory: Path, name: str, rows: list[dict]) -> None:
    (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class BugResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        with tempfile.TemporaryDirectory() as raw:
            a, b = Path(raw, "a"), Path(raw, "b")
            a.write_text(BUGGY, encoding="utf-8")
            b.write_text(FIXED, encoding="utf-8")
            diff = subprocess.run(
                ["git", "diff", "--no-index", "-U0", str(a), str(b)], capture_output=True, text=True, check=False
            ).stdout
        self.fix = {
            "commit": "c" * 40,
            "parent": "p" * 40,
            "author_date": "2026-01-02T00:00:00+00:00",
            "committer_date": "2026-01-02T00:00:00+00:00",
            "ai_marker": None,
            "files": {PATH: {"status": "M", "fixed": FIXED, "buggy": BUGGY, "diff_u0": diff}},
        }
        fixed = FIXED.encode()
        origin = {"tool": "mutmut", "native_id": "m", "operator": "op"}
        # An untouched-code witness (qty * rate -> qty + rate), an inserted-line witness (qty < 0 -> qty <= 0) and a
        # survivor that is no witness.
        self.untouched = from_mutated(PATH, fixed, fixed.replace(b"qty * rate", b"qty + rate"), origin)
        self.inserted = from_mutated(PATH, fixed, fixed.replace(b"qty < 0", b"qty <= 0"), origin)
        self.plain = from_mutated(PATH, fixed, fixed.replace(b"return 0", b"return 1"), origin)
        arts = [self.untouched, self.inserted, self.plain]
        write(self.dir, "mutmut.jsonl", [a.to_json() for a in arts])
        (self.dir / "mutmut.ledger.json").write_text(
            json.dumps(
                {"failed": False, "emitted": 3, "materialized": 3, "in_scope": 3, "unmaterializable_in_scope": 0}
            )
        )
        (self.dir / "bug.json").write_text(
            json.dumps(
                {
                    "subject": "pkg",
                    "commit": "c" * 40,
                    "parent": "p" * 40,
                    "src_files": [{"path": PATH, "status": "M"}],
                    "src_diff": "",
                    "t_fix": ["t::fault"],
                }
            )
        )
        (self.dir / "scope.json").write_text(json.dumps({PATH: [[1, 5, "function", "price"]]}))
        witness = {
            "status": "survived",
            "witness": True,
            "fix_red": {"t::fault": ["failed", "AssertionError"]},
            "covering": 1,
        }
        write(
            self.dir,
            "oracle.jsonl",
            [
                {
                    "kind": "meta",
                    "bug": "c" * 40,
                    "covered_lines": {PATH: [1, 2, 3, 5]},
                    "fix_covered_lines": {},
                    "t_fix": ["t::fault"],
                },
                {"aid": self.untouched.aid, "lines": [2, 2], **witness},
                {"aid": self.inserted.aid, "lines": [3, 3], **witness},
                {"aid": self.plain.aid, "lines": [4, 4], "status": "survived", "witness": False, "covering": 0},
            ],
        )
        write(
            self.dir,
            "oracle.confirm.jsonl",
            [{"aid": self.untouched.aid, **witness}, {"aid": self.inserted.aid, **witness}],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def result(self) -> metrics.BugResult:
        result = metrics.bug_result(self.dir, "x", self.fix, {"t::fault"}, ("mutmut",))
        assert result is not None
        return result

    def test_witnesses_and_their_classes(self) -> None:
        tb = self.result().tools["mutmut"]
        self.assertEqual((tb.witnesses, tb.survivors, tb.detected), (2, 3, True))
        self.assertEqual(tb.witness_classes, {"unchanged": 1, "inserted": 1})
        self.assertEqual(tb.best_class, "unchanged")
        self.assertTrue(tb.detected_unchanged_executed)

    def test_the_third_condition_needs_a_judged_counterpart(self) -> None:
        tb = self.result().tools["mutmut"]
        self.assertFalse(tb.detected_prefix)
        self.assertEqual(tb.prefix_undecided, 1)  # the untouched-code witness; the inserted one is never pre-fix
        parent = from_mutated(
            PATH,
            BUGGY.encode(),
            BUGGY.encode().replace(b"qty * rate", b"qty + rate"),
            {"tool": "mutmut", "native_id": "m"},
        )
        (self.dir / "prefix.json").write_text(
            json.dumps(
                {
                    "witnesses": {
                        self.untouched.aid: {
                            "class": "unchanged",
                            "tools": {
                                "mutmut": {"exists": "transplant", "parent": [{"aid": parent.aid, "path": PATH}]}
                            },
                        }
                    }
                }
            )
        )
        write(
            self.dir,
            "oracle.parent.jsonl",
            [
                {"kind": "meta", "mode": "parent", "covered_lines": {PATH: [1, 2, 3]}},
                {"aid": parent.aid, "status": "survived", "witness": None},
            ],
        )
        tb = self.result().tools["mutmut"]
        self.assertTrue(tb.detected_prefix)
        self.assertEqual((tb.prefix_witnesses, tb.prefix_undecided), (1, 0))
        write(
            self.dir,
            "oracle.parent.jsonl",
            [{"kind": "meta", "mode": "parent", "covered_lines": {}}, {"aid": parent.aid, "status": "killed"}],
        )
        self.assertFalse(self.result().tools["mutmut"].detected_prefix)

    def test_a_degenerate_generation_makes_the_tool_unavailable(self) -> None:
        (self.dir / "mutmut.ledger.json").write_text(
            json.dumps(
                {"failed": False, "emitted": 9, "materialized": 3, "in_scope": 3, "unmaterializable_in_scope": 6}
            )
        )
        tb = self.result().tools["mutmut"]
        self.assertFalse(tb.available)
        self.assertIn("6 of 9", tb.unavailable_reason or "")


if __name__ == "__main__":
    unittest.main()
