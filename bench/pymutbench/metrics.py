"""Per-bug and per-tool measures of the real-fault track, from the oracle's verdicts.

For tool ``x`` and bug ``b``, ``M`` is the tool's native in-scope mutants (one entry per emission; two
emissions of the same mutated file share one artifact and one verdict). Mutants of scope regions that hold no
change of the program (``fixlines.noise``: import statements, module metadata, definitions whose code the fix did
not change) are left out of every measure and published apart.

A **witness** of the fixed program meets two conditions: the historical suite does not kill the mutant and a
fail-to-pass test that checks the fault kills it; it counts only when an independent re-judgement from fresh trees
(``oracle.confirm.jsonl``) repeats it. A **pre-fix witness** also meets the third condition: the same mutant exists
in the buggy parent and the historical suite misses it there too (``oracle.parent.jsonl``): a mutant the tool could
have shown before the fix. A witness on lines the fix inserted cannot exist before it; one in code the fix did not
touch is written into the buggy file (``fixlines.transplant``); one on lines the fix rewrote exists when the tool's
own generator, run on the buggy file, emits the same change at the corresponding place (``prefix``).

The per-bug measures, for pre-fix witnesses (the primary ones) and for witnesses of the fixed program:

* ``detected`` — at least one witness among the tool's mutants (unrestricted volume);
* ``R(q)`` — the probability that ``q`` emissions drawn uniformly without replacement from ``M`` contain a
  witness (a fixed generation budget, which controls for the number of mutants a tool emits);
* ``S(k)`` — the probability that ``k`` artifacts drawn uniformly without replacement from the unique survivors
  of the tool's mutants, as the reference oracle judges them, contain a witness (a fixed inspection budget; the
  tool's own engine may report other survivors, which the engine track measures); variants draw only survivors
  on lines the historical suite executes, or lines with a survivor instead of survivors;
* both draw ``min(budget, population)`` items and are 0 for an empty population;
* the accounting of every emission: invalid, no-op, TCE-equivalent, gated out, killed, survived, unresolved;
* where the witnesses lie: on lines the fix inserted, rewrote or left unchanged (``fixlines.classify``), and what
  the historical suite executes there: a test (``test``), only the import of the module (``import``) or nothing
  (``never``).

Two coverage strata, both retrospective (they use the fix's scope and fail-to-pass tests, which did not exist
before the fix): a bug is coverage-guided when the fail-to-pass tests execute a line of the scope that the
historical suite never ran, and its fault lines are covered when every line the fix changed that the
fail-to-pass run executes was also executed by the historical suite (fail-to-pass coverage includes every
fail-to-pass test, feature tests too).
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import fixlines
from pymutbench.registry import REGISTRY
from pymutbench.registry import TOOLS as REGISTRY_TOOLS
from pymutbench.scope import changed_lines_in
from pymutbench.stats import hit_probability

TOOLS = REGISTRY_TOOLS
# A tool's row keeps the emissions of its default configuration: balamut's generation covers the source-mutating
# kinds `balamut diff` runs by default (src, concurrency, fault, extreme), and its row keeps the kinds `balamut
# mutate` runs by default (src, concurrency, fault), the file-level default every other tool gets and the one of
# its engine-track campaign (``tools.toml``: ``default_drop``).
DEFAULT_DROP: dict[str, str] = {name: tool.default_drop for name, tool in REGISTRY.items() if tool.default_drop}
Q_BUDGETS = (1, 5, 10, 25, 50, 100)
K_BUDGETS = (1, 3, 5, 10)
# A tool whose emissions in a bug's scope mostly have no faithful source text is unavailable for that bug: its
# adapter, not the tool, decided the result there.
UNMATERIALIZABLE_LIMIT = 0.5


@dataclass
class ToolBug:
    tool: str
    available: bool = True  # False when the generation adapter failed for this bug, or its output is degenerate
    unavailable_reason: str | None = None
    emitted: int = 0
    unique: int = 0
    unmaterializable: int = 0  # in-scope emissions without a faithful source text (in ``emitted``)
    status: Counter[str] = field(default_factory=Counter)
    witnesses: int = 0  # unique witness artifacts of the fixed program
    witness_emissions: int = 0
    survivors: int = 0  # unique survivor artifacts (witnesses included)
    detected: bool = False  # a witness of the fixed program (two conditions)
    detected_any_fix_test: bool = False  # sensitivity: a witness by any fail-to-pass test, feature tests included
    # The witness flow, in unique artifacts: first-judgement witnesses; of those, re-judgements without an outcome
    # (unknown), re-judgements that killed the mutant or found no red fail-to-pass test (unconfirmed), witnesses
    # whose only red outcome is a collection or startup failure of the whole fail-to-pass run in either judgement
    # (synthetic), and confirmed witnesses red only on tests of a feature the commit added (feature only).
    first_witnesses: int = 0
    unknown_confirmations: int = 0
    unconfirmed_witnesses: int = 0
    synthetic_witnesses: int = 0
    feature_only_witnesses: int = 0
    unknown_witness: int = 0  # survivors whose fail-to-pass run gave no outcome
    undecided: int = 0  # artifacts that could still be witnesses: unknown fix outcome or confirmation, unresolved
    exact_fault: bool | None = None  # single-file fixes only: some mutant re-creates the fault (``_reproduces``)
    r: dict[int, float] = field(default_factory=dict)
    s: dict[int, float] = field(default_factory=dict)
    # Where the witnesses lie and what the historical suite executes there (unique witness artifacts).
    witness_classes: dict[str, int] = field(default_factory=dict)
    witness_execution: dict[str, int] = field(default_factory=dict)
    best_class: str | None = None  # the best class of the tool's witnesses: unchanged, rewritten, inserted
    detected_existing: bool = False  # a witness whose place existed before the fix (unchanged or rewritten)
    detected_unchanged: bool = False  # a witness in code the fix did not touch
    detected_unchanged_executed: bool = False  # ... on a line a historical test executes
    detected_tested: bool = False  # a witness on a line a historical test executes
    # The third condition.
    prefix_witnesses: int = 0  # unique pre-fix witness artifacts
    prefix_undecided: int = 0  # witnesses whose buggy-side counterpart has no decided verdict
    detected_prefix: bool = False
    detected_prefix_tested: bool = False
    prefix_emissions: int = 0
    r_prefix: dict[int, float] = field(default_factory=dict)
    s_prefix: dict[int, float] = field(default_factory=dict)
    s_prefix_executed: dict[int, float] = field(default_factory=dict)  # survivors on lines the suite executes
    s_prefix_lines: dict[int, float] = field(default_factory=dict)  # lines with a survivor as the unit


@dataclass
class BugResult:
    subject: str
    commit: str
    fault_class: str
    repair_shape: str
    scope_lines: int
    fault_covered_by_old: bool
    coverage_guided: bool
    unresolved: int
    tools: dict[str, ToolBug]
    files: int = 1
    region_kind: str = "function"  # function, statement (class body or module level) or mixed
    confirmed: bool = True  # every first-judgement witness has a fresh re-judgement
    fault_lines_run: bool = True  # the fail-to-pass run executed a changed line (``fault_covered_by_old`` needs one)
    composition: str = "mixed"  # what the fix's hunks do: insertion, rewrite or mixed
    changed_lines: int = 0  # lines of the fixed files the fix inserted or rewrote
    committer_date: str = ""
    ai_marker: str | None = None
    noise_regions: list[list[Any]] = field(default_factory=list)  # [path, start, end, reason]
    # Not published per bug: the witness artifacts with their class and execution, for the buggy-side stage.
    details: dict[str, Any] = field(default_factory=dict, repr=False)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def repair_shape(diff: str) -> str:
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    if added and not removed:
        return "pure_addition"
    if removed and not added:
        return "pure_deletion"
    return "modification"


def noise_regions(scope: dict[str, list[list[Any]]], fix: dict[str, Any]) -> list[list[Any]]:
    found = []
    for path, regions in sorted(scope.items()):
        record = fix["files"].get(path, {})
        if record.get("fixed") is None:
            continue
        for start, end, *_ in regions:
            reason = fixlines.noise(record["fixed"], record.get("buggy"), record.get("diff_u0", ""), start, end)
            if reason:
                found.append([path, start, end, reason])
    return found


def execution(verdict: dict[str, Any], covered: set[int], touched: set[int]) -> str:
    """What the historical suite executes on the lines a judged mutant changes (``touched``): ``never`` when no
    context ran any of them, ``test`` when a test ran its edit (``covering``), else ``import`` (only the import of the
    module or the class body ran them). A multi-line edit whose other lines a test runs but whose changed lines no
    context runs is ``never``."""
    if not touched & covered:
        return "never"
    return "test" if verdict.get("covering", 0) > 0 else "import"


def bug_result(directory: Path, fault_class: str, fix: dict[str, Any], fault_tests: set[str] | None = None,
               tools_measured: tuple[str, ...] = TOOLS) -> BugResult | None:
    """The measures of one bug for ``tools_measured``; ``fix`` is its ``fixes`` record (sources and diffs)."""
    bug = json.loads((directory / "bug.json").read_text(encoding="utf-8"))
    oracle_rows = _jsonl(directory / "oracle.jsonl")
    metas = [r for r in oracle_rows if r.get("kind") == "meta"]
    meta = metas[-1] if metas else None
    verdicts = {r["aid"]: r for r in oracle_rows if "aid" in r}
    confirmations = {r["aid"]: r for r in _jsonl(directory / "oracle.confirm.jsonl") if "aid" in r}
    if meta is None or "ineligible" in meta:
        return None  # an ineligible bug is excluded from the corpus and reported in the exclusion ledger
    scope = json.loads((directory / "scope.json").read_text(encoding="utf-8"))
    noise = noise_regions(scope, fix)
    scope_lines = {path: {ln for start, end, *_ in regions for ln in range(start, end + 1)}
                   for path, regions in scope.items()}
    old_cov = {p: set(v) for p, v in meta.get("covered_lines", {}).items()}
    fix_cov = {p: set(v) for p, v in meta.get("fix_covered_lines", {}).items()}
    guided = any((fix_cov.get(p, set()) & lines) - old_cov.get(p, set()) for p, lines in scope_lines.items())
    fault_lines = {(path, ln) for path in scope for ln in changed_lines_in(bug.get("src_diff", ""), path)
                   if ln in fix_cov.get(path, set())}
    fault_covered = bool(fault_lines) and all(ln in old_cov.get(path, set()) for path, ln in fault_lines)
    files = fix["files"]
    sources = {p: (r["fixed"].encode("utf-8"), r["buggy"] if r["buggy"] is not None else "")
               for p, r in files.items() if r.get("fixed") is not None}
    canon = {p: _Canon(fixed, text) for p, (fixed, text) in sources.items()}
    changed = {p: changed_lines_in(bug.get("src_diff", ""), p) for p in sources}
    split = {p: fixlines.fix_lines(r.get("diff_u0", "")) for p, r in files.items()}
    reproduced: dict[str, bool] = {}  # by artifact: identical mutated files of several tools are checked once
    classes: dict[str, str] = {}
    executions: dict[str, str] = {}
    lines_of: dict[str, tuple[str, int]] = {}
    prefix = _Prefix(directory)

    def reproduces(row: dict[str, Any]) -> bool:
        if row["aid"] not in reproduced:
            reproduced[row["aid"]] = _reproduces(row, canon, changed)
        return reproduced[row["aid"]]

    touched_of: dict[str, set[int]] = {}

    def touched(row: dict[str, Any]) -> set[int]:
        if row["aid"] not in touched_of:
            fixed = sources.get(row["path"], (b"", ""))[0]
            edit = row["edit"]
            touched_of[row["aid"]] = fixlines.touched_lines(fixed, edit["start"], edit["end"], edit["replacement"])
        return touched_of[row["aid"]]

    def witness_class(row: dict[str, Any]) -> str:
        if row["aid"] not in classes:
            inserted, rewritten = split.get(row["path"], (set(), set()))
            classes[row["aid"]] = fixlines.classify(touched(row), inserted, rewritten)
        return classes[row["aid"]]

    def executed(row: dict[str, Any]) -> str:
        if row["aid"] not in executions:
            executions[row["aid"]] = execution(verdicts[row["aid"]], old_cov.get(row["path"], set()), touched(row))
        return executions[row["aid"]]

    def line_of(row: dict[str, Any]) -> tuple[str, int]:
        if row["aid"] not in lines_of:
            lines_of[row["aid"]] = (row["path"], min(touched(row)))
        return lines_of[row["aid"]]

    tools: dict[str, ToolBug] = {}
    unresolved_aids: set[str] = set()
    witness_detail: dict[str, dict[str, Any]] = {}
    for tool in tools_measured:
        drop = DEFAULT_DROP.get(tool, "")
        rows = _jsonl(directory / f"{tool}.jsonl")
        if drop:
            rows = [row for row in rows
                    if not any(o.get("tool") == tool and str(o.get("native_id", "")).startswith(drop)
                               for o in row.get("origins", []))]
        if noise:
            rows = [row for row in rows if not _in_noise(row, verdicts, noise)]
        ledger_path = directory / f"{tool}.ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"failed": True}
        if tool == "mutation":
            rows = [row for row in rows if not _gated(row, old_cov)]  # what the tool itself would generate
        lost = int(ledger.get("unmaterializable_in_scope", 0))  # emissions without a faithful text still cost
        reason = _unavailable(ledger)
        tb = ToolBug(tool, available=reason is None, unavailable_reason=reason, emitted=len(rows) + lost,
                     unique=len({row["aid"] for row in rows}), unmaterializable=lost)
        witness_aids: set[str] = set()
        survivor_aids: set[str] = set()
        undecided_aids: set[str] = set()
        by_aid = {row["aid"]: row for row in rows}
        for aid in by_aid:
            verdict = verdicts.get(aid, {"status": "missing"})
            tb.status[verdict["status"]] += 1
            if verdict["status"] == "survived":
                survivor_aids.add(aid)
                if verdict.get("witness"):
                    tb.first_witnesses += 1
                    second = confirmations.get(aid, {"status": "missing"})
                    if second["status"] in {"unresolved", "missing"} or (
                            second["status"] == "survived" and second.get("witness") is None):
                        tb.unknown_confirmations += 1
                        undecided_aids.add(aid)
                        continue
                    if not (second["status"] == "survived" and second.get("witness")):
                        tb.unconfirmed_witnesses += 1
                        continue
                    # The same fail-to-pass test red in both judgements, each time with an outcome of its own: an
                    # unattributed collection or startup failure marks every fail-to-pass test without any running.
                    red = _attributed_red(verdict) & _attributed_red(second)
                    if not red:
                        tb.synthetic_witnesses += 1
                        continue
                    tb.detected_any_fix_test = True
                    # A witness must be killed by a test of the fault itself, not by a test of a feature that
                    # the same commit added (``fault_tests`` from the purity labels; all tests when absent).
                    if fault_tests is None or red & fault_tests:
                        witness_aids.add(aid)
                    else:
                        tb.feature_only_witnesses += 1
                elif verdict.get("witness") is None and tb_has_fix(meta):
                    tb.unknown_witness += 1
                    undecided_aids.add(aid)
            elif verdict["status"] in {"unresolved", "missing"}:
                unresolved_aids.add(aid)
                undecided_aids.add(aid)  # an undecided historical outcome could still hide a witness
        tb.witness_emissions = sum(1 for row in rows if row["aid"] in witness_aids)
        tb.witnesses, tb.survivors, tb.undecided = len(witness_aids), len(survivor_aids), len(undecided_aids)
        tb.detected = tb.witnesses > 0
        tb.r = {q: hit_probability(tb.emitted, tb.witness_emissions, q) for q in Q_BUDGETS}
        tb.s = {k: hit_probability(tb.survivors, tb.witnesses, k) for k in K_BUDGETS}
        # Where the witnesses lie, what runs them, and whether they existed before the fix.
        cls = {aid: witness_class(by_aid[aid]) for aid in witness_aids}
        run = {aid: executed(by_aid[aid]) for aid in witness_aids}
        tb.witness_classes = dict(Counter(cls.values()))
        tb.witness_execution = dict(Counter(run.values()))
        tb.best_class = fixlines.best(set(cls.values()))
        tb.detected_existing = tb.best_class in {"unchanged", "rewritten"}
        tb.detected_unchanged = tb.best_class == "unchanged"
        tb.detected_unchanged_executed = any(cls[a] == "unchanged" and run[a] == "test" for a in witness_aids)
        tb.detected_tested = any(run[a] == "test" for a in witness_aids)
        states = {aid: prefix.state(tool, aid, cls[aid]) for aid in witness_aids}
        prefix_aids = {aid for aid, state in states.items() if state == "yes"}
        tb.prefix_witnesses = len(prefix_aids)
        tb.prefix_undecided = sum(1 for state in states.values() if state == "unknown")
        tb.detected_prefix = bool(prefix_aids)
        tb.detected_prefix_tested = any(run[a] == "test" for a in prefix_aids)
        tb.prefix_emissions = sum(1 for row in rows if row["aid"] in prefix_aids)
        tb.r_prefix = {q: hit_probability(tb.emitted, tb.prefix_emissions, q) for q in Q_BUDGETS}
        tb.s_prefix = {k: hit_probability(tb.survivors, tb.prefix_witnesses, k) for k in K_BUDGETS}
        executed_survivors = {a for a in survivor_aids if executed(by_aid[a]) != "never"}
        tb.s_prefix_executed = {k: hit_probability(len(executed_survivors), len(prefix_aids & executed_survivors), k)
                                for k in K_BUDGETS}
        survivor_lines = {line_of(by_aid[a]) for a in survivor_aids}
        witness_lines = {line_of(by_aid[a]) for a in prefix_aids}
        tb.s_prefix_lines = {k: hit_probability(len(survivor_lines), len(witness_lines), k) for k in K_BUDGETS}
        if len(bug.get("src_files", [])) == 1:  # a deleted file is a changed file too
            tb.exact_fault = any(reproduces(row) for row in rows)
        for aid in witness_aids:
            entry = witness_detail.setdefault(aid, {"class": cls[aid], "execution": run[aid],
                                                    "path": by_aid[aid]["path"],
                                                    "edit": by_aid[aid]["edit"], "tools": {}})
            entry["tools"][tool] = {"origins": [o for o in by_aid[aid].get("origins", []) if o.get("tool") == tool],
                                    "prefix": states[aid]}
        tools[tool] = tb
    kinds = {"function" if region[2] == "function" else "statement" for regions in scope.values() for region in regions}
    first_witnesses = {aid for aid, v in verdicts.items() if v.get("status") == "survived" and v.get("witness")}
    inserted_all = sum(len(split[p][0]) for p in split)
    rewritten_all = sum(len(split[p][1]) for p in split)
    return BugResult(bug["subject"], bug["commit"], fault_class, repair_shape(bug.get("src_diff", "")),
                     sum(len(v) for v in scope_lines.values()), fault_covered, guided, len(unresolved_aids), tools,
                     files=len(scope), region_kind=kinds.pop() if len(kinds) == 1 else "mixed",
                     confirmed=first_witnesses <= confirmations.keys(), fault_lines_run=bool(fault_lines),
                     composition=fixlines.composition([r.get("diff_u0", "") for r in files.values()]),
                     changed_lines=inserted_all + rewritten_all, committer_date=fix.get("committer_date", ""),
                     ai_marker=fix.get("ai_marker"), noise_regions=noise, details={"witnesses": witness_detail})


class _Prefix:
    """The buggy-side evidence of a bug's witnesses (``prefix.json``, ``oracle.parent.jsonl``)."""

    def __init__(self, directory: Path) -> None:
        path = directory / "prefix.json"
        self.record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        rows = _jsonl(directory / "oracle.parent.jsonl")
        self.verdicts = {r["aid"]: r for r in rows if "aid" in r}
        metas = [r for r in rows if r.get("kind") == "meta" and "ineligible" not in r]
        self.covered = {p: set(v) for p, v in (metas[-1].get("covered_lines", {}) if metas else {}).items()}
        self.ineligible = bool(rows) and not metas

    def state(self, tool: str, aid: str, cls: str) -> str:
        """``yes`` (a pre-fix witness), ``no`` or ``unknown`` for a witness of the fixed program."""
        if cls == "inserted":
            return "no"
        entry = self.record.get("witnesses", {}).get(aid, {}).get("tools", {}).get(tool)
        if entry is None or entry.get("exists") == "failed":
            return "unknown"
        if entry.get("exists") == "absent":
            return "no"
        open_ = False
        for counterpart in entry.get("parent", []):
            verdict = self.verdicts.get(counterpart["aid"], {}).get("status")
            if verdict == "survived" and not self._gated(counterpart):
                return "yes"
            open_ = open_ or verdict not in {"killed", "gated_out", "invalid", "equivalent_tce", "noop", "survived"}
        return "unknown" if open_ or not entry.get("parent") else "no"

    def _gated(self, counterpart: dict[str, Any]) -> bool:
        """A coverage-gated tool (``mutation``) would not generate the counterpart: its line never ran there."""
        line = counterpart.get("gate_line")
        return line is not None and line not in self.covered.get(counterpart["path"], set())


def _unavailable(ledger: dict[str, Any]) -> str | None:
    if ledger.get("failed", False):
        return f"generation failed: {ledger.get('failure')}"
    lost = int(ledger.get("unmaterializable_in_scope", 0))
    if ledger.get("emitted", 0) and not ledger.get("materialized", 0) and lost:
        return "no emission has a faithful source text"
    total = lost + int(ledger.get("in_scope", 0))
    if total and lost / total > UNMATERIALIZABLE_LIMIT:
        return f"{lost} of {total} in-scope emissions have no faithful source text"
    return None


def _in_noise(row: dict[str, Any], verdicts: dict[str, dict[str, Any]], noise: list[list[Any]]) -> bool:
    first, last = verdicts.get(row["aid"], {}).get("lines", [0, -1])
    return any(path == row["path"] and start <= first and last <= end for path, start, end, _ in noise)


def tb_has_fix(meta: dict[str, Any]) -> bool:
    return bool(meta.get("t_fix"))


SYNTHETIC_RED = {"collect", "startup"}  # the oracle's marks for a failure of the whole fail-to-pass run


def _attributed_red(verdict: dict[str, Any]) -> set[str]:
    """The fail-to-pass tests a judgement recorded red with an outcome of their own (failed, errored or crashed when
    they ran in the fail-to-pass run), not through a collection or startup failure of the whole run."""
    return {test for test, outcome in verdict.get("fix_red", {}).items() if outcome[0] not in SYNTHETIC_RED}


def _gated(row: dict[str, Any], old_cov: dict[str, set[int]]) -> bool:
    """A ``mutation`` emission the tool itself would not produce: its node line was never executed."""
    lines = [o.get("gate_line") for o in row.get("origins", []) if o.get("tool") == "mutation" and "gate_line" in o]
    return bool(lines) and all(line not in old_cov.get(row["path"], set()) for line in lines)


class _Canon:
    """The fixed and the buggy file of a fix as syntax-tree dumps per top-level statement (positions ignored): a
    mutant's file is compared with the buggy one by dumping only the statements its edit touches, the others being
    the fixed file's own. Equal lists of statement dumps are equal module trees."""

    def __init__(self, fixed: bytes, buggy: str) -> None:
        self.fixed = fixed
        self.lines = fixed.count(b"\n")
        try:
            tree = ast.parse(fixed.decode("utf-8"))
            buggy_tree = ast.parse(buggy)
        except (SyntaxError, UnicodeDecodeError):
            self.spans: list[tuple[int, int]] | None = None
            return
        self.spans = [(_first_line(node), node.end_lineno or node.lineno) for node in tree.body]
        self.dumps = [ast.dump(node, include_attributes=False) for node in tree.body]
        self.buggy = [ast.dump(node, include_attributes=False) for node in buggy_tree.body]

    def equals_buggy(self, first: int, last: int, mutated: bytes) -> bool:
        """Whether ``mutated`` (the fixed file with an edit of fixed lines ``first``..``last``) parses to the buggy
        file's tree."""
        if self.spans is None:
            return False
        touched = [i for i, (start, end) in enumerate(self.spans) if start <= last and first <= end]
        if not touched:
            return self.dumps == self.buggy  # an edit between statements leaves the fixed program as it is
        i, j = touched[0], touched[-1]
        start, end = self.spans[i][0], self.spans[j][1] + mutated.count(b"\n") - self.lines
        if end < start:
            return False
        snippet = b"\n".join(mutated.split(b"\n")[start - 1 : end])
        try:
            statements = ast.parse(snippet.decode("utf-8")).body
        except (SyntaxError, UnicodeDecodeError, ValueError):
            return False
        dumps = [*self.dumps[:i], *(ast.dump(node, include_attributes=False) for node in statements),
                 *self.dumps[j + 1 :]]
        return dumps == self.buggy


def _first_line(node: ast.stmt) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno, *(d.lineno for d in decorators)])


def _reproduces(row: dict[str, Any], canon: dict[str, _Canon], changed: dict[str, set[int]]) -> bool:
    """The mutant re-creates the real fault where the fault is: its edit touches a line the fix changed
    (``changed``, fixed-file lines) and turns the fixed file into the buggy one (equal syntax trees). The line test
    comes first, and only the statements the edit touches are parsed again."""
    if row["path"] not in canon:
        return False
    target = canon[row["path"]]
    edit = row["edit"]
    first = target.fixed.count(b"\n", 0, edit["start"]) + 1
    last = target.fixed.count(b"\n", 0, max(edit["start"], edit["end"] - 1)) + 1
    if not any(first <= line <= last for line in changed.get(row["path"], ())):
        return False
    mutated = target.fixed[: edit["start"]] + edit["replacement"].encode("utf-8") + target.fixed[edit["end"] :]
    return target.equals_buggy(first, last, mutated)


def to_json(results: list[BugResult]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        row = asdict(result)
        row.pop("details", None)
        row["tools"] = {name: {**asdict(tb), "status": dict(sorted(tb.status.items()))}
                        for name, tb in result.tools.items()}
        rows.append(row)
    return rows
