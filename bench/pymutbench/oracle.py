"""The reference oracle: judges every normalized mutant of one bug in fresh pytest processes (container side).

For bug ``b`` with fixed tree ``F`` the protocol per artifact ``m`` is:

0. **Materialize.** Apply the edit to the original bytes. A mutated file identical to the original is a no-op;
   one that does not compile is ``invalid`` (a generation defect); one whose bytecode equals the original's
   (line tables ignored) is ``equivalent_tce`` and is not executed.
1. **Covering subset.** Run the old tests that executed the mutated lines on the original, with ``-x``. A red
   test kills only if the same subset is green on the unmutated tree (the paired baseline, cached per subset).
2. **Old suite.** Otherwise run the whole historical suite in its canonical order, with ``-x``. Any red test,
   collection error, crash or mutation-induced hang kills; a complete green run is a survivor. Coverage only
   orders the work: survival is always established by the complete suite.
3. **Fix tests.** A survivor runs the fail-to-pass tests of the fix in the fixed tree. A red fix test makes the
   mutant a **coupling witness** of the bug: a mutant the historical suite misses and the bug's own regression
   test kills (Just et al. 2014; Kintis et al. 2018).

Each run verifies that the subject's packages were imported from the judged tree; a run that did not finish
for a reason the mutant cannot explain is ``unresolved``, never a verdict.

The parent side (``--side parent``) judges mutants of the buggy parent's source with stages 0 to 2 in the parent
tree (the historical suite against the buggy source): a survivor there is a mutant the historical suite also
misses before the fix.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.artifacts import Artifact, compiled_key, edit_lines, read_jsonl  # noqa: E402
from pymutbench.fingerprint import current_semantics, fingerprint, oracle_semantics  # noqa: E402
from pymutbench.gitops import FileChange, add_worktree, remove_worktree, take_files  # noqa: E402
from pymutbench.pytestrun import RED, PytestEnv, RunResult, run_pytest  # noqa: E402
from pymutbench.subjects import SUBJECTS, Subject  # noqa: E402
from pymutbench.validate import env_for, origin_ok, write_generated  # noqa: E402


class Worker:
    """A private pair of trees (``old`` = historical tests on the fixed source, ``fixed`` = the fix commit)."""

    def __init__(self, index: int, repo: Path, base: Path, subject: Subject, bug: dict[str, Any]) -> None:
        self.old = base / f"w{index}" / "old"
        self.fixed = base / f"w{index}" / "fixed"
        changes = [FileChange(f["status"], f["path"], f["added"], f["removed"]) for f in bug["src_files"]]
        add_worktree(repo, self.old, bug["parent"])
        take_files(self.old, bug["commit"], changes, from_side="fix")
        add_worktree(repo, self.fixed, bug["commit"])
        for tree in (self.old, self.fixed):
            write_generated(tree, subject)
        self.env_old = env_for(self.old, subject)
        self.env_fixed = env_for(self.fixed, subject)

    def trees(self) -> list[Path]:
        return [self.old, self.fixed]


class ParentWorker:
    """A private ``parent`` tree: the historical suite against the buggy source (no fix tree)."""

    def __init__(self, index: int, repo: Path, base: Path, subject: Subject, bug: dict[str, Any]) -> None:
        self.old = base / f"w{index}" / "parent"
        self.fixed: Path | None = None
        add_worktree(repo, self.old, bug["parent"])
        write_generated(self.old, subject)
        self.env_old = env_for(self.old, subject)
        self.env_fixed = self.env_old

    def trees(self) -> list[Path]:
        return [self.old]


FINGERPRINT = fingerprint("oracle")  # the code this process loaded, fixed at start


def _unkilled(run: Any) -> RunResult:
    """A run, repeated once when the environment killed it (SIGKILL from outside the harness): an out-of-memory
    kill of the container VM is not evidence about the code under test."""
    result = run()
    return run() if result.external_kill else result


class Ineligible(RuntimeError):
    """The unmutated trees do not meet the protocol: no mutant of this bug may be judged."""


class SuiteWorker:
    """A private copy of a project judged with its own default pytest invocation (no fix tree)."""

    def __init__(self, index: int, root: Path, base: Path, env: PytestEnv, import_root: str) -> None:
        self.old = base / f"w{index}"
        shutil.copytree(root, self.old, symlinks=True)
        self.fixed: Path | None = None
        self.env_old = PytestEnv(env.python, (str(self.old / import_root),), env.origin_packages, env.extra_args,
                                 env.per_test_timeout)
        self.env_fixed = self.env_old


class Oracle:
    def __init__(self, repo: Path, work: Path, subject: Subject | None, bug: dict[str, Any], jobs: int,
                 suite: tuple[Path, PytestEnv, str, tuple[str, ...]] | None = None, side: str = "fixed") -> None:
        self.repo, self.subject, self.bug = repo, subject, bug
        self.base = work / str(bug["commit"])[:12]
        self.lock = threading.Lock()
        if suite is None:
            assert subject is not None
            kind = ParentWorker if side == "parent" else Worker
            self.workers: list[Any] = [kind(i, repo, self.base, subject, bug) for i in range(jobs)]
            self.roots = [root for root in subject.tests if (self.workers[0].old / root).exists()]
        else:
            root, env, import_root, roots = suite
            self.workers = [SuiteWorker(i, root, self.base, env, import_root) for i in range(jobs)]
            # The test roots the native campaigns run, so a reference verdict rests on the same tests; none: the
            # project's own invocation (its configured test paths).
            self.roots = list(roots)
        self.free: list[Any] = list(self.workers)
        self.excluded = list(bug.get("t_old_excluded", []))
        self.t_fix = list(bug.get("t_fix", []))
        self.suite_seconds = float(bug.get("old_suite_seconds", 30.0))
        self.subset_cache: dict[frozenset[str], set[str]] = {}
        # Tests that killed earlier mutants of a file, most frequent first: tried alone before the subset.
        self.killers: dict[str, Counter[str]] = {}
        self.coverage: dict[str, dict[str, set[int]]] = {}  # relpath -> test -> lines
        self.originals: dict[str, bytes] = {}
        self.compiled: dict[str, Any] = {}
        self.covered: dict[str, list[int]] = {}
        self.expected_tests = 0
        self.full_only = False

    def close(self) -> None:
        for worker in self.workers:
            if isinstance(worker, Worker | ParentWorker):
                for tree in worker.trees():
                    remove_worktree(self.repo, tree)
        shutil.rmtree(self.base, ignore_errors=True)

    def screen(self, runs: int) -> dict[str, Any]:
        """Suite mode: run the unmutated suite ``runs`` times; only tests green in every run may kill."""
        w0 = self.workers[0]
        results = [run_pytest(w0.env_old, w0.old, self.roots, run_timeout=3600) for _ in range(runs)]
        seen = set().union(*(set(r.tests) for r in results))
        stable = set.intersection(*(r.green() for r in results))
        flaky = sorted(n for n in seen - stable if any(n in r.green() for r in results))
        always_red = sorted(seen - stable - set(flaky))
        self.excluded = sorted(seen - stable)
        self.suite_seconds = max(r.wall for r in results)
        return {"screen_runs": runs, "tests": len(seen), "flaky": flaky, "always_red": always_red,
                "suite_seconds": self.suite_seconds}

    def prepare(self, paths: list[str]) -> dict[str, Any]:
        w0 = self.workers[0]
        for path in paths:
            data = (w0.old / path).read_bytes()
            self.originals[path] = data
            self.compiled[path] = compiled_key(data.decode("utf-8"), path)
        cover = [str(w0.old / path) for path in paths]
        run = _unkilled(lambda: run_pytest(w0.env_old, w0.old, self.roots, run_timeout=6 * self.suite_seconds + 120,
                                           deselect=self.excluded, cover_files=cover))
        if not run.finished or not origin_ok(run, w0.old) or run.collect_errors:
            raise Ineligible(f"historical baseline: finished={run.finished} origin_ok={origin_ok(run, w0.old)} "
                             f"collect_errors={sorted(run.collect_errors)[:3]} external_kill={run.external_kill}")
        newly_red = sorted(run.red())
        if newly_red:  # unstable now: such a test can neither kill nor prove survival
            self.excluded = sorted(set(self.excluded) | set(newly_red))
        self.expected_tests = len(run.green())
        if not self.expected_tests:
            raise Ineligible("historical baseline: no green test")
        prefix = str(w0.old) + "/"
        for test, files in run.lines.items():
            for abspath, lines in files.items():
                self.coverage.setdefault(abspath.removeprefix(prefix), {})[test] = set(lines)
        red = sorted(run.red())
        self.covered = {path: sorted({line for lines in tests.values() for line in lines})
                        for path, tests in self.coverage.items()}
        if w0.fixed is None or not self.t_fix:
            return {"baseline_finished": run.finished, "baseline_red": red[:20], "baseline_wall": run.wall,
                    "origin_ok": origin_ok(run, w0.old), "covered_lines": self.covered,
                    "expected_tests": self.expected_tests, "excluded_now_red": newly_red}
        # The regression tests' own coverage of the same files, in the fixed tree (the coverage baseline).
        fix_cover = [str(w0.fixed / path) for path in paths]
        fix_run = _unkilled(lambda: run_pytest(w0.env_fixed, w0.fixed, self.t_fix,
                                               run_timeout=6 * self.suite_seconds + 120, cover_files=fix_cover))
        if not fix_run.finished or not origin_ok(fix_run, w0.fixed):
            raise Ineligible(f"fix baseline: finished={fix_run.finished} origin_ok={origin_ok(fix_run, w0.fixed)} "
                             f"external_kill={fix_run.external_kill}")
        failing_fix = sorted(n for n in self.t_fix if fix_run.tests.get(n) is None or n in fix_run.red())
        self.t_fix = [n for n in self.t_fix if n not in failing_fix]  # red on the fixed tree: cannot witness
        if not self.t_fix:
            raise Ineligible(f"fix baseline: every fail-to-pass test fails on the fixed tree {failing_fix[:3]}")
        fixed_prefix = str(w0.fixed) + "/"
        fix_covered: dict[str, set[int]] = {}
        for files in fix_run.lines.values():
            for abspath, lines in files.items():
                fix_covered.setdefault(abspath.removeprefix(fixed_prefix), set()).update(lines)
        return {"baseline_finished": run.finished, "baseline_red": red[:20], "baseline_wall": run.wall,
                "origin_ok": origin_ok(run, w0.old), "covered_lines": self.covered,
                "expected_tests": self.expected_tests, "excluded_now_red": newly_red,
                "fix_covered_lines": {path: sorted(lines) for path, lines in fix_covered.items()},
                "t_fix": self.t_fix, "t_fix_dropped": failing_fix}

    def gated_out(self, art: Artifact) -> bool:
        """True when every origin is a coverage-gated tool whose node line the historical suite never ran:
        the tool would not have generated this mutant (``mutation`` mutates covered lines only)."""
        gated = [origin for origin in art.origins if "gate_line" in origin]
        if not gated or len(gated) != len(art.origins):
            return False
        covered = set(self.covered.get(art.path, []))
        return all(origin["gate_line"] not in covered for origin in gated)

    def _take(self) -> Worker:
        while True:
            with self.lock:
                if self.free:
                    return self.free.pop()
            time.sleep(0.01)

    def _give(self, worker: Worker) -> None:
        with self.lock:
            self.free.append(worker)

    def covering(self, path: str, first: int, last: int) -> list[str]:
        excluded = set(self.excluded)
        tests = self.coverage.get(path, {})
        hit = {test for test, lines in tests.items() if test != "<collect>" and any(first <= ln <= last for ln in lines)}
        return sorted(hit - excluded)

    def subset_baseline(self, worker: Worker, subset: list[str], path: str, mutated: bytes) -> set[str]:
        """The tests of ``subset`` that are green on the unmutated tree when run as that subset (cached)."""
        key = frozenset(subset)
        with self.lock:
            cached = self.subset_cache.get(key)
        if cached is not None:
            return cached
        (worker.old / path).write_bytes(self.originals[path])
        try:
            run = _unkilled(lambda: run_pytest(worker.env_old, worker.old, subset, run_timeout=self.run_timeout))
        finally:
            (worker.old / path).write_bytes(mutated)
        green = run.green() if run.finished and run.exitstatus in {0, 1} else set()
        if not run.external_kill:  # a baseline the environment killed twice is no evidence, and not remembered
            with self.lock:
                self.subset_cache[key] = green
        return green

    def judge(self, art: Artifact) -> dict[str, Any]:
        started = time.monotonic()
        original = self.originals[art.path]
        mutated = art.apply(original)
        record: dict[str, Any] = {"aid": art.aid, "path": art.path}
        first, last = edit_lines(original, art.edit)
        record["lines"] = [first, last]
        if mutated == original:
            return {**record, "status": "noop", "seconds": 0.0}
        try:
            text = mutated.decode("utf-8")
            compile(text, art.path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            return {**record, "status": "invalid", "reason": f"{type(exc).__name__}: {exc}"[:200], "seconds": 0.0}
        if compiled_key(text, art.path) == self.compiled[art.path]:
            return {**record, "status": "equivalent_tce", "seconds": 0.0}
        if self.gated_out(art):
            return {**record, "status": "gated_out", "seconds": 0.0}
        worker = self._take()
        try:
            record.update(self._execute(worker, art.path, mutated, first, last))
        finally:
            (worker.old / art.path).write_bytes(original)
            if worker.fixed is not None:
                (worker.fixed / art.path).write_bytes(original)
            self._give(worker)
        record["seconds"] = round(time.monotonic() - started, 2)
        return record

    @property
    def run_timeout(self) -> float:
        """One budget for every run of a mutant and for its paired baselines."""
        return 3 * self.suite_seconds + 60

    @staticmethod
    def _evidence(run: RunResult) -> tuple[str, list[str]]:
        """What a run shows: ``kill`` (red tests, attributed), ``unattributed`` (collection errors, or a stop
        before any test ran: needs a repeat), ``clean`` (finished, exit status 0) or ``unknown`` (including a run
        the environment killed: it shows nothing about the mutant)."""
        if run.external_kill:
            return "unknown", ["<external-kill>"]
        red = sorted(run.red())
        if red:
            return "kill", red
        if run.collect_errors:
            return "unattributed", [f"<collect:{node}>" for node in sorted(run.collect_errors)]
        if not run.finished:
            return "unattributed", ["<startup-timeout>" if run.returncode in {-9, 137} else f"<startup rc={run.returncode}>"]
        if run.exitstatus != 0:
            return "unknown", [f"<exitstatus {run.exitstatus}>"]
        return "clean", []

    def _judged_run(self, worker: Any, tree: Path, env: PytestEnv, targets: list[str], *, exitfirst: bool,
                    deselect: list[str] | None = None) -> tuple[str, list[str], RunResult]:
        """A run and its evidence; an unattributed failure counts only if a repeat of the same run shows it again,
        and a run the environment killed is repeated once before it counts as unknown."""

        def once() -> RunResult:
            return _unkilled(lambda: run_pytest(env, tree, targets, exitfirst=exitfirst, deselect=deselect,
                                                run_timeout=self.run_timeout))

        run = once()
        if run.finished and not origin_ok(run, tree):
            return "unknown", ["<import-origin>"], run
        evidence, killers = self._evidence(run)
        if evidence == "unattributed":
            again = once()
            evidence_again, killers_again = self._evidence(again)
            if evidence_again != "unattributed":
                return evidence_again, killers_again, again
        return evidence, killers, run

    def _execute(self, worker: Any, path: str, mutated: bytes, first: int, last: int) -> dict[str, Any]:
        covering = self.covering(path, first, last)
        out: dict[str, Any] = {"covering": len(covering)}
        (worker.old / path).write_bytes(mutated)
        with self.lock:
            ranked = [t for t, _ in self.killers.get(path, Counter()).most_common() if t in set(covering)][:2]
        if self.full_only:  # the sensitivity mode: the canonical suite alone decides
            ranked, covering = [], []
        for test in ranked:
            # Stage 0: the likely killer alone. A kill counts only if the same single-test run is green on the
            # unmutated tree (cached per test, same budget).
            evidence, killers, run = self._judged_run(worker, worker.old, worker.env_old, [test], exitfirst=False)
            if evidence in {"kill", "unattributed"} and test in self.subset_baseline(worker, [test], path, mutated):
                self._credit(path, test)
                return {**out, "status": "killed", "stage": "killer", "killed_by": killers[:3],
                        "kill_kind": self._kind(run, killers[0])}
        if covering:
            # Stage 1: the covering subset with -x, paired with the same subset on the unmutated tree.
            evidence, killers, run = self._judged_run(worker, worker.old, worker.env_old, covering, exitfirst=True)
            if evidence in {"kill", "unattributed"}:
                green = self.subset_baseline(worker, covering, path, mutated)
                confirmed = [k for k in killers if k in green] if evidence == "kill" else (killers if green else [])
                if confirmed:
                    self._credit(path, confirmed[0])
                    return {**out, "status": "killed", "stage": "subset", "killed_by": confirmed[:3],
                            "kill_kind": self._kind(run, confirmed[0])}
        # Stage 2: the whole historical suite in its canonical order.
        evidence, killers, run = self._judged_run(worker, worker.old, worker.env_old, self.roots, exitfirst=True,
                                                  deselect=self.excluded)
        if evidence in {"kill", "unattributed"}:
            return {**out, "status": "killed", "stage": "full", "killed_by": killers[:3],
                    "kill_kind": self._kind(run, killers[0])}
        if evidence == "unknown":
            return {**out, "status": "unresolved", "reason": killers[0] if killers else "unknown"}
        passed = len(run.green())
        if self.expected_tests and passed != self.expected_tests:
            return {**out, "status": "unresolved", "reason": f"inventory {passed}/{self.expected_tests}"}
        if worker.fixed is None or not self.t_fix:
            return {**out, "status": "survived", "witness": None}
        # Stage 3: the bug's fail-to-pass tests in the fixed tree.
        (worker.old / path).write_bytes(self.originals[path])
        (worker.fixed / path).write_bytes(mutated)
        evidence, killers, fix_run = self._judged_run(worker, worker.fixed, worker.env_fixed, self.t_fix,
                                                      exitfirst=False)
        if evidence == "unknown":
            return {**out, "status": "survived", "witness": None, "reason": f"fix_run {killers[:1]}"}
        red_fix = {n: (fix_run.tests[n].outcome, fix_run.tests[n].exc_type) for n in self.t_fix
                   if n in fix_run.tests and fix_run.tests[n].outcome in RED}
        if evidence == "unattributed":
            red_fix.update({n: ("collect" if killers[0].startswith("<collect") else "startup", "")
                            for n in self.t_fix if n not in red_fix})
        return {**out, "status": "survived", "witness": bool(red_fix),
                "fix_red": {n: list(v) for n, v in red_fix.items()}, "fix_total": len(self.t_fix)}

    def _credit(self, path: str, test: str) -> None:
        if test.startswith("<"):
            return
        with self.lock:
            self.killers.setdefault(path, Counter())[test] += 1

    @staticmethod
    def _kind(run: RunResult, killer: str) -> str:
        if killer.startswith("<"):
            return killer.strip("<>").split(":")[0].split()[0]
        test = run.tests.get(killer)
        return f"{test.outcome}:{test.exc_type}" if test else "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subject", default="", help="bug mode: the subject")
    parser.add_argument("--bug", default="", help="bug mode: a JSON file with one admitted bug record")
    parser.add_argument("--mutants", required=True, help="the normalized artifacts (JSONL)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", default="/work/repo")
    parser.add_argument("--work", default="/work/oracle")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--suite-root", default="", help="suite mode: judge with this project's own pytest run")
    parser.add_argument("--python", default="/work/venv/bin/python", help="suite mode: the reference interpreter")
    parser.add_argument("--import-root", default="src", help="suite mode: the import root inside the project")
    parser.add_argument("--packages", default="", help="suite mode: comma-separated import names")
    parser.add_argument("--tests", default="",
                        help="suite mode: comma-separated test roots (default: the project's configured test paths)")
    parser.add_argument("--screen-runs", type=int, default=5)
    parser.add_argument("--full-only", action="store_true", help="skip stages 0 and 1 (oracle sensitivity runs)")
    parser.add_argument("--witnesses-of", default="",
                        help="judge only the witnesses recorded in this oracle output (their confirmation)")
    parser.add_argument("--side", choices=("fixed", "parent"), default="fixed",
                        help="bug mode: mutants of the fixed source (default) or of the buggy parent's source")
    args = parser.parse_args()
    if args.suite_root:
        main_suite(args)
        return
    subject = SUBJECTS[args.subject]
    bug = json.loads(Path(args.bug).read_text(encoding="utf-8"))
    artifacts = read_jsonl(Path(args.mutants))
    if args.witnesses_of:
        first = {row["aid"]: row for row in _rows(Path(args.witnesses_of)) if "aid" in row}
        artifacts = [art for art in artifacts if first.get(art.aid, {}).get("witness")]
    out = Path(args.out)
    previous = _rows(out)
    mode = "parent" if args.side == "parent" else "bug"
    semantics = current_semantics(mode)
    other = {oracle_semantics(row) for row in previous if row.get("kind") == "meta"} - {semantics}
    if other:
        raise SystemExit(f"{out} holds verdicts of other oracle semantics {sorted(other)}: remove it to judge again")
    stamp = {"semantics": semantics, "fingerprint": FINGERPRINT, "image": os.environ.get("PMB_IMAGE", "")}
    if mode == "parent":
        stamp = {"mode": "parent", **stamp}
    status = {row["aid"]: row["status"] for row in previous if "aid" in row}
    oracle = Oracle(Path(args.repo), Path(args.work), subject, bug, args.jobs, side=args.side)
    oracle.full_only = args.full_only
    try:
        try:
            meta = oracle.prepare(sorted({art.path for art in artifacts}))
        except Ineligible as exc:
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "meta", "bug": bug["commit"], "ineligible": str(exc), **stamp}) + "\n")
            return
        # A verdict is reused unless it is a coverage gate that no longer applies: the artifact gained an origin.
        todo = [art for art in artifacts if art.aid not in status
                or (status[art.aid] == "gated_out" and not oracle.gated_out(art))]
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "meta", "bug": bug["commit"], **stamp, "full_only": args.full_only,
                                 "witnesses_of": args.witnesses_of, **meta}) + "\n")
            fh.flush()
            with ThreadPoolExecutor(args.jobs) as pool:
                for record in pool.map(oracle.judge, todo):
                    fh.write(json.dumps(record) + "\n")
                    fh.flush()
    finally:
        oracle.close()


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def main_suite(args: argparse.Namespace) -> None:
    """Suite mode: reference verdicts for native campaigns, on the test roots they run."""
    root = Path(args.suite_root)
    packages = tuple(p for p in args.packages.split(",") if p)
    roots = tuple(t for t in args.tests.split(",") if t)
    env = PytestEnv(args.python, (str(root / args.import_root),), packages, ("-W", "ignore::pytest.PytestRemovedIn10Warning"))
    artifacts = read_jsonl(Path(args.mutants))
    oracle = Oracle(Path("/nonexistent"), Path(args.work), None, {"commit": root.name or "suite"}, args.jobs,
                    suite=(root, env, args.import_root, roots))
    out = Path(args.out)
    try:
        screen = oracle.screen(args.screen_runs)
        meta = oracle.prepare(sorted({art.path for art in artifacts}))
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "meta", "mode": "suite", "semantics": current_semantics("suite"),
                                 "fingerprint": FINGERPRINT, "image": os.environ.get("PMB_IMAGE", ""),
                                 "test_roots": list(roots), **screen, **meta}) + "\n")
            with ThreadPoolExecutor(args.jobs) as pool:
                for record in pool.map(oracle.judge, artifacts):
                    fh.write(json.dumps(record) + "\n")
                    fh.flush()
    finally:
        oracle.close()


if __name__ == "__main__":
    main()
