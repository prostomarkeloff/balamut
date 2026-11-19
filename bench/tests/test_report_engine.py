"""The engine report's comparison of repeated campaigns."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymutbench.report_engine import stability, varied


def run(*mutants: tuple[str | None, str, str]) -> dict[str, Any]:
    return {"mutants": [{"path": "pkg/m.py", "aid": aid, "native_id": native_id, "verdict": verdict}
                        for aid, native_id, verdict in mutants]}


class RepeatedCampaignTest(unittest.TestCase):
    def test_mutants_are_matched_by_their_mutated_file(self) -> None:
        # mutation numbers its mutants anew in every run: the same mutated file is the same mutant.
        first = run(("a1", "01a1-1", "killed"), ("a2", "01a1-2", "survived"))
        second = run(("a1", "01b2-1", "killed"), ("a2", "01b2-2", "timeout"))
        self.assertEqual(stability(first, second), {"common": 2, "changed": 1, "only_first": 0, "only_second": 0})

    def test_unmapped_mutants_are_matched_by_file_and_native_id(self) -> None:
        first = run((None, "7", "killed"), ("a1", "8", "killed"))
        second = run((None, "7", "survived"), ("a3", "9", "killed"))
        self.assertEqual(stability(first, second), {"common": 1, "changed": 1, "only_first": 1, "only_second": 1})

    def test_native_mutants_of_one_file_keep_all_their_verdicts(self) -> None:
        first = run(("a1", "1", "killed"))
        self.assertEqual(stability(first, run(("a1", "1", "killed"), ("a1", "2", "killed")))["changed"], 0)
        self.assertEqual(stability(first, run(("a1", "1", "killed"), ("a1", "2", "survived")))["changed"], 1)

    def test_varied_counts_each_mutant_every_run_has_once(self) -> None:
        runs = [run(("a1", "1", "survived"), ("a2", "2", "killed"), ("a3", "3", "killed")),
                run(("a1", "1", "timeout"), ("a2", "2", "killed"), ("a3", "3", "survived")),
                run(("a1", "1", "survived"), ("a2", "2", "killed"))]
        self.assertEqual(varied(runs), {"survived/timeout": 1})


if __name__ == "__main__":
    unittest.main()
