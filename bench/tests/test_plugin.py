"""The reference pytest plugin through ``pytestrun``: how each kind of test outcome is recorded.

Needs an interpreter with pytest (the repository's environment: ``uv run python -m unittest``); skipped without.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymutbench.pytestrun import RED, PytestEnv, run_pytest

TESTS = """
import time
import unittest

import pytest


class Cases(unittest.TestCase):
    def test_failed_subtest(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown")


def test_skip_then_teardown_error(broken_teardown):
    pytest.skip("skipped")


@pytest.mark.xfail(reason="known")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="known")
def test_xpass():
    assert True


def test_pass():
    assert True


def test_hang():
    time.sleep(120)
"""


@unittest.skipUnless(importlib.util.find_spec("pytest"), "needs pytest")
class PluginTest(unittest.TestCase):
    def test_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_cases.py").write_text(textwrap.dedent(TESTS), encoding="utf-8")
            env = PytestEnv(sys.executable, (str(root),), (), per_test_timeout=3.0)
            result = run_pytest(env, root, ["tests"], run_timeout=60)
        outcome = {nodeid.split("::")[-1]: test.outcome for nodeid, test in result.tests.items()}
        self.assertIn(outcome["test_failed_subtest"], RED)
        self.assertIn(outcome["test_skip_then_teardown_error"], RED)
        self.assertEqual(outcome["test_xfail"], "xfailed")
        self.assertEqual(outcome["test_xpass"], "xpassed")
        self.assertEqual(outcome["test_pass"], "passed")
        # faulthandler writes its dump into pytest's captured output, where the runner does not see it: a hang is
        # recorded as a crash of the hanging test. Both are red (docs/benchmark.md, Threats to validity: Hangs).
        self.assertEqual(outcome["test_hang"], "crashed")
        self.assertEqual(result.crashed.split("::")[-1], "test_hang")


if __name__ == "__main__":
    unittest.main()
