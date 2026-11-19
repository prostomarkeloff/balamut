"""Aggregate the real-fault track into the published results: data files, tables and figures.

    python3 -m pymutbench.report bugs --results bench/results --images docs/images [--focus TOOL]

Per tool and measure (``MEASURES``; the primary one is detection by a pre-fix witness, the three-condition
measure): the share of bugs with a witness pooled over bugs, its 95% percentile interval from a bootstrap over
projects and the interval of the tool's rank over the same resamples; the budgeted measures R(q) and S(k); the
accounting of every emitted mutant; where the witnesses lie and what the historical suite executes there; and the
paired difference of every pair of tools with a bootstrap p-value, Holm-adjusted over all pairs. No tool is
singled out unless ``--focus`` names one, which only highlights it in the figures.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import random
import sys
from collections import Counter
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import CORPUS, RESULTS, __version__, fixes, svg
from pymutbench.fingerprint import oracle_mode, oracle_semantics
from pymutbench.metrics import K_BUDGETS, Q_BUDGETS, TOOLS, BugResult, bug_result, to_json
from pymutbench.orchestrate import BENCH, OUT, WORK
from pymutbench.registry import REGISTRY
from pymutbench.stats import SEED, Bootstrap, cluster_bootstrap, holm, pooled_mean, project_mean, spearman, split_half

PRIMARY = "prefix"
# Bug-level measures: key -> (ToolBug flag, what a detected bug has).
MEASURES: dict[str, tuple[str, str]] = {
    "prefix": ("detected_prefix", "a pre-fix witness: the three conditions"),
    "fixed": ("detected", "a witness of the fixed program: the two conditions"),
    "existing": ("detected_existing", "a witness whose place existed before the fix (unchanged or rewritten lines)"),
    "unchanged": ("detected_unchanged", "a witness in code the fix did not touch"),
    "unchanged_executed": ("detected_unchanged_executed", "a witness in untouched code on a line a test executes"),
    "tested": ("detected_tested", "a witness on a line a historical test executes"),
    "prefix_tested": ("detected_prefix_tested", "a pre-fix witness on a line a historical test executes"),
}
RELIABILITY = ("prefix", "fixed", "existing", "unchanged", "unchanged_executed")  # compared as headline candidates
# Budgeted measures with a replicate series: key -> (ToolBug attribute, budget).
BUDGET_SERIES: dict[str, tuple[str, int]] = {
    "r10": ("r_prefix", 10),
    "s3": ("s_prefix", 3),
    "r1": ("r_prefix", 1),
    "s1": ("s_prefix", 1),
    "r10_fixed": ("r", 10),
    "s3_fixed": ("s", 3),
}
LITE_SUITE_LIMIT = 60.0  # seconds: projects whose median historical suite runs longer are left out of Lite
LITE_PER_PROJECT = 5


def purity() -> dict[str, set[str]]:
    """subject@commit12 -> the fail-to-pass tests that check the fault (the purity labels)."""
    found: dict[str, set[str]] = {}
    for path in sorted((OUT / "review" / "purity").glob("batch-*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            found[item["id"]] = set(item.get("fault_tests", []))
    return found


def load(
    *, accept_incomplete: frozenset[str] = frozenset(), preview: bool = False, tools: tuple[str, ...] = TOOLS
) -> list[BugResult]:
    """The results of every selected bug. Publication requires a final and complete selection (or a subject
    named in ``accept_incomplete``), complete generation and judging of every selected bug, a fresh re-judgement
    of every witness, a judged buggy-side counterpart of every witness that can have one, and the judged
    oracle samples; a preview reports the gaps instead of refusing."""
    fault_tests = purity()
    labels = {row["id"]: row for row in _rows(OUT / "review" / "final.jsonl")}
    selection = json.loads((OUT / "bugs" / "selection.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    if selection.get("provisional"):
        problems.append("the selection uses provisional labels")
    for name, row in sorted(selection["subjects"].items()):
        if not row["complete"] and name not in accept_incomplete:
            problems.append(f"{name}: selection incomplete, open: {row['open'][:3]}")
    results = []
    excluded: list[dict[str, str]] = []
    unavailable: list[dict[str, Any]] = []
    fingerprints: dict[str, set[str]] = {}
    ORACLE_CODE.clear()
    for subject, row in sorted(selection["subjects"].items()):
        bugs_file = OUT / "bugs" / f"{subject}.jsonl"
        listed = len(_rows(bugs_file))
        if listed != row["selected"]:
            problems.append(f"{subject}: {listed} bugs listed in {bugs_file.name}, {row['selected']} selected")
    for subject in sorted(selection["subjects"]):
        for bug in _rows(OUT / "bugs" / f"{subject}.jsonl"):
            directory = OUT / "gen" / subject / bug["commit"][:12]
            cid = f"{subject}@{bug['commit'][:12]}"
            if labels.get(cid, {}).get("label") != "bug":
                problems.append(f"{cid}: selected without a final bug label")
                continue
            if cid not in fault_tests:
                problems.append(f"{cid}: no purity decision (which fail-to-pass tests check the fault)")
                continue
            missing = [t for t in tools if not (directory / f"{t}.ledger.json").exists()]
            if missing:
                problems.append(f"{cid}: no generation ledger for {missing}")
                continue
            judged = _judging_gap(directory) or _tool_gap(directory, tools)
            if judged:
                problems.append(f"{cid}: {judged}")
                continue
            fault = labels[cid].get("fault_class", "unlabelled")
            meta = [r for r in _rows(directory / "oracle.jsonl") if r.get("kind") == "meta"][-1]
            if "ineligible" in meta:
                excluded.append(
                    {"id": cid, "reason": f"the oracle found the unmutated trees ineligible: {meta['ineligible']}"}
                )
                continue
            # Only validated fail-to-pass tests that the oracle kept (green on the fixed tree) can make a witness.
            tests_of_fault = fault_tests[cid] & set(bug.get("t_fix", [])) & set(meta.get("t_fix", []))
            if not tests_of_fault:
                excluded.append(
                    {
                        "id": cid,
                        "reason": "no fail-to-pass test that checks the fault is left after the oracle's baseline",
                    }
                )
                continue
            fix = fixes.ensure(directory, WORK / "repos" / subject, bug)
            result = bug_result(directory, fault, fix, tests_of_fault, tools)
            if result is None:
                problems.append(f"{cid}: no measures (judging incomplete)")
                continue
            if not result.confirmed:
                problems.append(f"{cid}: witnesses without a fresh re-judgement (pipeline confirm)")
            gap = _prefix_gap(directory, result)
            if gap:
                problems.append(f"{cid}: {gap}")
            results.append(result)
            _collect_fingerprints(directory, fingerprints)
            _collect_unavailable(directory, cid, result, unavailable)
    for stage, seen in fingerprints.items():
        if len(seen) > 1:
            problems.append(f"mixed {stage} {sorted(seen)}")
    problems += _sample_gap(
        "sensitivity", "oracle.full.jsonl", "the oracle-shortcut sensitivity sample", "pipeline sensitivity"
    )
    problems += _sample_gap(
        "equivalence", "oracle.equiv.jsonl", "the oracle equivalence sample", "pipeline equivalence"
    )
    if problems and not preview:
        raise SystemExit("not publishable:\n  " + "\n  ".join(problems[:40]) + f"\n  ({len(problems)} problems)")
    EXCLUDED[:] = excluded
    UNAVAILABLE[:] = unavailable
    PROBLEMS[:] = problems
    return results


def _judging_gap(directory: Path) -> str:
    """Why a bug's judging is incomplete, or "" when every artifact has a verdict (or the oracle refused the bug)."""
    rows = _rows(directory / "oracle.jsonl")
    metas = [r for r in rows if r.get("kind") == "meta"]
    if not metas:
        return "not judged"
    if "ineligible" in metas[-1]:
        return ""
    missing = {r["aid"] for r in _rows(directory / "union.jsonl")} - {r["aid"] for r in rows if "aid" in r}
    return f"{len(missing)} artifacts without a verdict" if missing else ""


def _tool_gap(directory: Path, tools: tuple[str, ...] = TOOLS) -> str:
    """Why some tool artifact has no verdict, or "": a union written before a tool's artifacts would hide them
    from ``_judging_gap``."""
    rows = _rows(directory / "oracle.jsonl")
    judged = {r["aid"] for r in rows if "aid" in r}
    missing = {row["aid"] for tool in tools for row in _rows(directory / f"{tool}.jsonl")} - judged
    return f"{len(missing)} tool artifacts without a verdict (stale union)" if missing else ""


def _prefix_gap(directory: Path, result: BugResult) -> str:
    """Why the third condition is not decided where it can be, or "": every witness on lines that existed before
    the fix needs its buggy-side counterparts built (``pipeline prefix``; rewritten lines after ``pipeline buggy``)
    and judged on the parent tree."""
    witnesses = result.details.get("witnesses", {})
    if not any(w["class"] != "inserted" for w in witnesses.values()):
        return ""
    record = (
        json.loads((directory / "prefix.json").read_text(encoding="utf-8"))
        if (directory / "prefix.json").exists()
        else None
    )
    if record is None or set(witnesses) - set(record.get("witnesses", {})):
        return "buggy-side counterparts not built (pipeline prefix)"
    pending = sum(
        1
        for aid, w in witnesses.items()
        for tool in w["tools"]
        if record["witnesses"][aid]["tools"].get(tool, {}).get("exists", "pending") == "pending"
    )
    if pending:
        return f"{pending} witnesses on rewritten lines without buggy-side generation (pipeline buggy, then prefix)"
    rows = _rows(directory / "oracle.parent.jsonl")
    if any(r.get("kind") == "meta" and "ineligible" in r for r in rows) and not any(
        r.get("kind") == "meta" and "ineligible" not in r for r in rows
    ):
        return ""  # the parent baseline is ineligible: the counterparts stay undecided, and are counted so
    unjudged = {r["aid"] for r in _rows(directory / "parent.union.jsonl")} - {r["aid"] for r in rows if "aid" in r}
    return f"{len(unjudged)} buggy-side counterparts without a verdict (pipeline prefix)" if unjudged else ""


def _sample_gap(name: str, filename: str, what: str, command: str) -> list[str]:
    """A documented oracle sample must be complete: every sampled mutant judged again."""
    path = OUT / "bugs" / f"{name}.json"
    if not path.exists():
        return [f"{what} is not drawn ({command})"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    by_bug: dict[tuple[str, str], set[str]] = {}
    for subject, commit12, aid in manifest["sample"]:
        by_bug.setdefault((subject, commit12), set()).add(aid)
    missing = sum(
        len(aids - {r["aid"] for r in _rows(OUT / "gen" / s / c / filename) if "aid" in r})
        for (s, c), aids in by_bug.items()
    )
    return [f"{missing} mutants of {what} without a verdict ({command})"] if missing else []


ORACLE_CODE: dict[str, set[str]] = {}  # oracle semantics -> the code fingerprints of its records (published)
EXCLUDED: list[dict[str, str]] = []  # bugs dropped at reporting time, with the reason (published)
UNAVAILABLE: list[dict[str, Any]] = []  # (tool, bug) pairs a tool's generation did not deliver, with the evidence
PROBLEMS: list[str] = []  # publication gaps, reported by a preview


def _collect_unavailable(directory: Path, cid: str, result: BugResult, into: list[dict[str, Any]]) -> None:
    for tool, tb in sorted(result.tools.items()):
        if tb.available:
            continue
        ledger = json.loads((directory / f"{tool}.ledger.json").read_text(encoding="utf-8"))
        into.append(
            {
                "tool": tool,
                "bug": cid,
                "reason": tb.unavailable_reason,
                "rc": ledger.get("rc"),
                "failure": ledger.get("failure"),
                "attempts": ledger.get("attempts", 1),
                "emitted": ledger.get("emitted"),
                "materialized": ledger.get("materialized"),
                "unmaterializable_in_scope": ledger.get("unmaterializable_in_scope"),
                "unmaterializable": ledger.get("unmaterializable"),
                "stderr_tail": str(ledger.get("stderr", ""))[-600:],
            }
        )


def _collect_fingerprints(directory: Path, into: dict[str, set[str]]) -> None:
    """A tool's generation must come from one code; the oracle's verdicts of one mode from one semantics, whose
    code fingerprints are published (``ORACLE_CODE``)."""
    for ledger in directory.glob("*.ledger.json"):
        tool = ledger.name.removesuffix(".ledger.json")
        into.setdefault(f"generation of {tool}", set()).add(
            json.loads(ledger.read_text(encoding="utf-8")).get("fingerprint", "?")
        )
    for ledger in (directory / "buggy").glob("*.ledger.json"):
        tool = ledger.name.removesuffix(".ledger.json")
        into.setdefault(f"buggy-side generation of {tool}", set()).add(
            json.loads(ledger.read_text(encoding="utf-8")).get("fingerprint", "?")
        )
    for name in ("oracle.jsonl", "oracle.confirm.jsonl", "oracle.full.jsonl", "oracle.parent.jsonl"):
        for row in _rows(directory / name):
            if row.get("kind") == "meta":
                semantics = oracle_semantics(row)
                into.setdefault(f"oracle {oracle_mode(row)} semantics", set()).add(semantics)
                ORACLE_CODE.setdefault(semantics, set()).add(str(row.get("fingerprint", "?")))


def _clusters(results: list[BugResult], value: Callable[[BugResult], float]) -> dict[str, list[float]]:
    clusters: dict[str, list[float]] = {}
    for result in results:
        clusters.setdefault(result.subject, []).append(float(value(result)))
    return clusters


def _flag(result: BugResult, tool: str, flag: str) -> bool:
    """Operational: a tool unavailable for a bug (its generation failed or is degenerate there) detected nothing."""
    tb = result.tools[tool]
    return tb.available and bool(getattr(tb, flag))


def _detects(result: BugResult, tool: str, measure: str = PRIMARY) -> bool:
    return _flag(result, tool, MEASURES[measure][0])


def _budget(result: BugResult, tool: str, attribute: str, budget: int) -> float:
    tb = result.tools[tool]
    return getattr(tb, attribute)[budget] if tb.available else 0.0


def _period(result: BugResult) -> str:
    date = result.committer_date[:7]
    return "2021–2025" if date < "2026-01" else "2026 January–June" if date < "2026-07" else "2026 July–November"


def _size(result: BugResult) -> str:
    lines = result.changed_lines
    return "1–4 lines" if lines <= 4 else "5–12 lines" if lines <= 12 else "13 or more lines"


STRATA: dict[str, tuple[Callable[[BugResult], str], tuple[str, ...]]] = {
    "region": (lambda r: r.region_kind, ("function", "statement", "mixed")),
    "coverage": (
        lambda r: "covered" if r.fault_covered_by_old else "gap" if r.fault_lines_run else "unrun",
        ("covered", "gap", "unrun"),
    ),
    "composition": (lambda r: r.composition, ("insertion", "rewrite", "mixed")),
    "size": (_size, ("1–4 lines", "5–12 lines", "13 or more lines")),
    "period": (_period, ("2021–2025", "2026 January–June", "2026 July–November")),
    "ai_marker": (lambda r: "AI marker" if r.ai_marker else "no marker", ("AI marker", "no marker")),
}


def aggregate(results: list[BugResult], tools_measured: tuple[str, ...] = TOOLS) -> dict[str, Any]:
    """Every measure is operational over all bugs (a tool unavailable for a bug detects nothing there); the rate
    conditional on the tool's generation succeeding is reported beside the primary one."""
    projects = Counter(r.subject for r in results)
    boot = Bootstrap(list(projects))
    tools: dict[str, Any] = {}
    series: dict[str, dict[str, list[float]]] = {key: {} for key in (*MEASURES, *BUDGET_SERIES)}
    clusters_of: dict[str, dict[str, dict[str, list[float]]]] = {key: {} for key in series}
    for tool in tools_measured:
        available = [r for r in results if r.tools[tool].available]
        detection: dict[str, Any] = {}
        for key, (flag, _) in MEASURES.items():
            clusters = _clusters(results, lambda r, t=tool, f=flag: _flag(r, t, f))
            clusters_of[key][tool] = clusters
            series[key][tool] = boot.replicates(clusters)
            detection[key] = {
                "bugs": sum(1 for r in results if _flag(r, tool, flag)),
                "rate": pooled_mean(clusters),
                "ci": list(boot.interval(series[key][tool])),
            }
        for key, (attribute, budget) in BUDGET_SERIES.items():
            clusters = _clusters(results, lambda r, t=tool, a=attribute, b=budget: _budget(r, t, a, b))
            clusters_of[key][tool] = clusters
            series[key][tool] = boot.replicates(clusters)
        primary = clusters_of[PRIMARY][tool]
        conditional = _clusters(available, lambda r, t=tool: _detects(r, t))
        leave_one_out = [pooled_mean({p: v for p, v in primary.items() if p != project}) for project in primary]
        status: Counter[str] = Counter()
        classes: Counter[str] = Counter()
        execution: Counter[str] = Counter()
        for r in available:
            tb = r.tools[tool]
            status.update(tb.status)
            classes.update(tb.witness_classes)
            execution.update(tb.witness_execution)
        best_execution = Counter(_best_execution(r.tools[tool]) for r in available if r.tools[tool].detected)
        tools[tool] = {
            "bugs": len(results),
            "available_bugs": len(available),
            "unavailable_bugs": len(results) - len(available),
            "detection": detection,
            "rate_ci_bca": list(boot.bca(primary)),
            "rate_project_mean": project_mean(primary),
            "rate_leave_one_project_out": [min(leave_one_out), max(leave_one_out)] if leave_one_out else None,
            "rate_without_arrow": pooled_mean({p: v for p, v in primary.items() if p != "arrow"}),
            "rate_conditional": pooled_mean(conditional),
            "rate_conditional_ci": list(cluster_bootstrap(conditional, pooled_mean)),
            "r": {
                q: pooled_mean(_clusters(results, lambda r, t=tool, q=q: _budget(r, t, "r_prefix", q)))
                for q in Q_BUDGETS
            },
            "s": {
                k: pooled_mean(_clusters(results, lambda r, t=tool, k=k: _budget(r, t, "s_prefix", k)))
                for k in K_BUDGETS
            },
            "s_executed": {
                k: pooled_mean(_clusters(results, lambda r, t=tool, k=k: _budget(r, t, "s_prefix_executed", k)))
                for k in K_BUDGETS
            },
            "s_lines": {
                k: pooled_mean(_clusters(results, lambda r, t=tool, k=k: _budget(r, t, "s_prefix_lines", k)))
                for k in K_BUDGETS
            },
            "r_fixed": {
                q: pooled_mean(_clusters(results, lambda r, t=tool, q=q: _budget(r, t, "r", q))) for q in Q_BUDGETS
            },
            "s_fixed": {
                k: pooled_mean(_clusters(results, lambda r, t=tool, k=k: _budget(r, t, "s", k))) for k in K_BUDGETS
            },
            "r10_ci": list(boot.interval(series["r10"][tool])),
            "s3_ci": list(boot.interval(series["s3"][tool])),
            "emitted": sum(r.tools[tool].emitted for r in available),
            "unique": sum(r.tools[tool].unique for r in available),
            "unmaterializable": sum(r.tools[tool].unmaterializable for r in available),
            "status": dict(sorted(status.items())),
            "witness_mutants": sum(r.tools[tool].witnesses for r in available),
            "prefix_witness_mutants": sum(r.tools[tool].prefix_witnesses for r in available),
            "witness_classes": dict(sorted(classes.items())),
            "witness_execution": dict(sorted(execution.items())),
            "best_class": dict(
                sorted(Counter(r.tools[tool].best_class for r in available if r.tools[tool].detected).items())
            ),
            "best_execution": dict(sorted(best_execution.items())),
            "witness_flow": {
                field: sum(getattr(r.tools[tool], field) for r in available)
                for field in (
                    "first_witnesses",
                    "unknown_confirmations",
                    "unconfirmed_witnesses",
                    "synthetic_witnesses",
                    "feature_only_witnesses",
                    "witnesses",
                )
            },
            "undecided_mutants": sum(r.tools[tool].undecided for r in available),
            "undecided_bugs": sum(1 for r in available if not r.tools[tool].detected and r.tools[tool].undecided),
            "prefix_undecided_witnesses": sum(r.tools[tool].prefix_undecided for r in available),
            "prefix_undecided_bugs": sum(
                1 for r in available if not r.tools[tool].detected_prefix and r.tools[tool].prefix_undecided
            ),
            "survivors_per_bug_median": _median([r.tools[tool].survivors for r in available]),
            "emitted_per_bug_median": _median([r.tools[tool].emitted for r in available]),
            "exact_fault": sum(1 for r in available if r.tools[tool].exact_fault is True),
            "exact_fault_applicable": sum(1 for r in available if r.tools[tool].exact_fault is not None),
            "strata": {
                name: {value: _rate([r for r in results if key(r) == value], tool) for value in values}
                for name, (key, values) in STRATA.items()
            },
            # Optimistic: every artifact the oracle left undecided counted as a pre-fix witness.
            "rate_optimistic": sum(
                1
                for r in available
                if r.tools[tool].detected_prefix or r.tools[tool].undecided or r.tools[tool].prefix_undecided
            )
            / max(1, len(results)),
            "rate_any_fix_test": sum(1 for r in available if r.tools[tool].detected_any_fix_test)
            / max(1, len(results)),
        }
    for key in (*MEASURES, *BUDGET_SERIES):
        for tool, (low, high) in Bootstrap.ranks(series[key]).items():
            target = tools[tool]["detection"][key] if key in MEASURES else tools[tool].setdefault("ranks", {})
            if key in MEASURES:
                target["rank"] = [low, high]
            else:
                target[key] = [low, high]
    for tool in tools_measured:
        others = [o for o in tools_measured if o != tool]
        tools[tool]["only_this_tool"] = sum(
            1 for r in results if _detects(r, tool) and not any(_detects(r, o) for o in others)
        )
    pairs = _pairs(results, series, PRIMARY, tools_measured)
    reliability = {}
    for key in (*RELIABILITY, "r10", "s3", "r1", "s1"):
        paired = pairs if key == PRIMARY else _pairs(results, series, key, tools_measured)
        widths = sorted(p["ci"][1] - p["ci"][0] for p in paired)
        reliability[key] = {
            "separable_pairs": sum(1 for p in paired if p["ci"][0] > 0 or p["ci"][1] < 0),
            "pairs": len(paired),
            "median_pair_ci_width": widths[len(widths) // 2] if widths else math.nan,
            "split_half_spearman": split_half(clusters_of[key]),
        }
    overlap = [
        [sum(1 for r in results if _detects(r, a) and not _detects(r, b)) for b in tools_measured]
        for a in tools_measured
    ]
    guided = _clusters(results, lambda r: r.coverage_guided)
    return {
        "release": {"corpus": CORPUS, "harness": __version__, "results": RESULTS},
        "bugs": len(results),
        "projects": dict(sorted(projects.items())),
        "primary": PRIMARY,
        "measures": {key: label for key, (_, label) in MEASURES.items()},
        "oracle": {semantics: sorted(codes) for semantics, codes in sorted(ORACLE_CODE.items())},
        "tools": tools,
        "pairs": pairs,
        "reliability": reliability,
        "overlap": {"labels": list(tools_measured), "values": overlap},
        "union_rate": {
            key: sum(1 for r in results if any(_detects(r, t, key) for t in tools_measured)) / max(1, len(results))
            for key in MEASURES
        },
        "coverage_guided_rate": pooled_mean(guided),
        "coverage_guided_ci": list(boot.percentile(guided)),
        "coverage_guided_rank": _baseline_rank(boot, series[PRIMARY], guided),
        "fault_covered_by_old": sum(1 for r in results if r.fault_covered_by_old),
        "fault_coverage_gap": sum(1 for r in results if r.fault_lines_run and not r.fault_covered_by_old),
        "fault_lines_unrun": sum(1 for r in results if not r.fault_lines_run),
        "strata_sizes": {
            name: {value: sum(1 for r in results if key(r) == value) for value in values}
            for name, (key, values) in STRATA.items()
        },
        "repair_shapes": dict(sorted(Counter(r.repair_shape for r in results).items())),
        "fault_classes": dict(sorted(Counter(r.fault_class for r in results).items())),
        "by_fault_class": {
            cls: {tool: _rate([r for r in results if r.fault_class == cls], tool) for tool in tools_measured}
            for cls in sorted({r.fault_class for r in results})
        },
        "corpus": _corpus(results),
        "noise": {
            "bugs": sum(1 for r in results if r.noise_regions),
            "regions": dict(sorted(Counter(reason for r in results for *_, reason in r.noise_regions).items())),
        },
        "unresolved_mutants": sum(r.unresolved for r in results),
        "statistics": {
            "interval": "percentile bootstrap over projects, pointwise 95%",
            "replicates": len(boot.draws),
            "seed": SEED,
            "clusters": len(projects),
            "paired_test": "two-sided bootstrap p-value of the paired difference, Holm over all pairs",
            "minimum_detectable_difference": reliability[PRIMARY]["median_pair_ci_width"] / 2,
        },
    }


def _best_execution(tb: Any) -> str:
    run = tb.witness_execution
    return "test" if run.get("test") else "import" if run.get("import") else "never"


def _pairs(
    results: list[BugResult],
    series: dict[str, dict[str, list[float]]],
    key: str,
    tools_measured: tuple[str, ...] = TOOLS,
) -> list[dict[str, Any]]:
    """Every pair of tools on one measure: the paired difference, its interval and the bootstrap p-value,
    Holm-adjusted over all pairs; for a bug-level measure also the discordant bugs."""
    pairs = []
    for first, second in combinations(tools_measured, 2):
        if key in MEASURES:
            a = [float(_detects(r, first, key)) for r in results]
            b = [float(_detects(r, second, key)) for r in results]
        else:
            attribute, budget = BUDGET_SERIES[key]
            a = [_budget(r, first, attribute, budget) for r in results]
            b = [_budget(r, second, attribute, budget) for r in results]
        estimate = (sum(a) - sum(b)) / max(1, len(results))
        pair: dict[str, Any] = {
            "first": first,
            "second": second,
            **Bootstrap.difference(series[key][first], series[key][second], estimate),
        }
        pair["ci"] = list(pair["ci"])
        if key in MEASURES:
            pair["first_only"] = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
            pair["second_only"] = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
        pairs.append(pair)
    for pair, adjusted in zip(pairs, holm([p["p"] for p in pairs]), strict=True):
        pair["p_holm"] = adjusted
    return pairs


def _baseline_rank(boot: Bootstrap, primary: dict[str, list[float]], guided: dict[str, list[float]]) -> list[int]:
    """The rank interval the coverage-only baseline would take among the tools."""
    series = {**primary, "coverage only": boot.replicates(guided)}
    return list(Bootstrap.ranks(series)["coverage only"])


def _corpus(results: list[BugResult]) -> dict[str, Any]:
    return {
        "years": dict(sorted(Counter(r.committer_date[:4] for r in results).items())),
        "ai_marker_bugs": sum(1 for r in results if r.ai_marker),
        "ai_marker_by_project": dict(sorted(Counter(r.subject for r in results if r.ai_marker).items())),
        "composition": dict(sorted(Counter(r.composition for r in results).items())),
        "changed_lines_median": _median([r.changed_lines for r in results]),
    }


def sensitivity() -> dict[str, Any]:
    """The seeded sample of stage 0/1 kills (``bugs/sensitivity.json``, written by ``pipeline sensitivity``)
    judged again by the canonical suite alone: how many of the sampled kills the whole historical suite repeats."""
    manifest_path = OUT / "bugs" / "sensitivity.json"
    if not manifest_path.exists():
        return {"sampled": 0}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fault_tests = purity()
    outcome: Counter[str] = Counter()
    by_stage: dict[str, Counter[str]] = {}
    discordant: list[dict[str, Any]] = []
    for subject, commit12, aid in manifest["sample"]:
        directory = OUT / "gen" / subject / commit12
        first = {r["aid"]: r for r in _rows(directory / "oracle.jsonl") if "aid" in r}.get(aid, {})
        second = {r["aid"]: r for r in _rows(directory / "oracle.full.jsonl") if "aid" in r}.get(aid)
        verdict = second["status"] if second else "not judged"
        outcome[verdict] += 1
        by_stage.setdefault(first.get("stage", "?"), Counter())[verdict] += 1
        if second is not None and verdict != "killed":
            # A sampled kill the canonical suite does not repeat: which tools emitted it, and whether a test of the
            # fault is red on it (a candidate witness the staged oracle's shortcut hid; not a confirmed one).
            tools = sorted(
                {
                    o.get("tool", "?")
                    for row in _rows(directory / "union.jsonl")
                    if row["aid"] == aid
                    for o in row.get("origins", [])
                }
            )
            red = set(second.get("fix_red", {})) & fault_tests.get(f"{subject}@{commit12}", set())
            discordant.append(
                {
                    "bug": f"{subject}@{commit12}",
                    "aid": aid,
                    "first_stage": first.get("stage"),
                    "first_killed_by": first.get("killed_by", []),
                    "full_suite": verdict,
                    "reason": second.get("reason"),
                    "tools": tools,
                    "fault_tests_red": sorted(red),
                }
            )
    killed, survived = outcome.get("killed", 0), outcome.get("survived", 0)
    return {
        "seed": manifest["seed"],
        "population": manifest["population"],
        "sampled": len(manifest["sample"]),
        "bugs": len({(s, c) for s, c, _ in manifest["sample"]}),
        "full_suite": dict(sorted(outcome.items())),
        "by_first_stage": {k: dict(sorted(v.items())) for k, v in sorted(by_stage.items())},
        "agreement": killed / (killed + survived) if killed + survived else math.nan,
        "killed_of_sampled": killed / len(manifest["sample"]) if manifest["sample"] else math.nan,
        "discordant": discordant,
    }


def _by_code(rows: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], str]]:
    """The latest verdict of every artifact in a verdict file with the code fingerprint of the run that wrote it (the
    meta record that opens the run)."""
    found: dict[str, tuple[dict[str, Any], str]] = {}
    code = "?"
    for row in rows:
        if row.get("kind") == "meta":
            code = str(row.get("fingerprint", "?"))
        elif "aid" in row:
            found[row["aid"]] = (row, code)
    return found


def equivalence() -> dict[str, Any]:
    """The seeded sample of judged artifacts (``bugs/equivalence.json``, written by ``pipeline equivalence``)
    judged again in bug mode by the current oracle code: per artifact, whether the status and, for a survivor, the
    witness outcome repeat the recorded verdict, by the code that recorded it."""
    path = OUT / "bugs" / "equivalence.json"
    if not path.exists():
        return {"sampled": 0}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    outcome: dict[str, Counter[str]] = {}
    pairs: Counter[str] = Counter()
    discordant: list[dict[str, Any]] = []
    rejudged: set[str] = set()
    cache: dict[
        tuple[str, str], tuple[dict[str, tuple[dict[str, Any], str]], dict[str, tuple[dict[str, Any], str]]]
    ] = {}
    for subject, commit12, aid in manifest["sample"]:
        directory = OUT / "gen" / subject / commit12
        if (subject, commit12) not in cache:
            cache[(subject, commit12)] = (
                _by_code(_rows(directory / "oracle.jsonl")),
                _by_code(_rows(directory / "oracle.equiv.jsonl")),
            )
        first, code = cache[(subject, commit12)][0].get(aid, ({}, "?"))
        second_entry = cache[(subject, commit12)][1].get(aid)
        tally = outcome.setdefault(code, Counter())
        if second_entry is None:
            tally["not judged"] += 1
            continue
        second, second_code = second_entry
        rejudged.add(second_code)
        a, b = _verdict_key(first), _verdict_key(second)
        pairs[f"{a} -> {b}"] += 1
        if a == b:
            tally["same"] += 1
        elif a in {"unresolved", "survived: unknown"} or b in {"unresolved", "survived: unknown"}:
            tally["undecided in one judgement"] += 1
        else:
            tally["different"] += 1
            discordant.append(
                {
                    "bug": f"{subject}@{commit12}",
                    "aid": aid,
                    "recorded": a,
                    "rejudged": b,
                    "recorded_code": code,
                    "recorded_killed_by": first.get("killed_by", []),
                    "rejudged_killed_by": second.get("killed_by", []),
                    "reason": second.get("reason"),
                }
            )
    same = sum(t["same"] for t in outcome.values())
    decided = same + sum(t["different"] for t in outcome.values())
    return {
        "seed": manifest["seed"],
        "population": manifest["population"],
        "sampled": len(manifest["sample"]),
        "bugs": len({(s, c) for s, c, _ in manifest["sample"]}),
        "outcome_by_recorded_code": {code: dict(sorted(t.items())) for code, t in sorted(outcome.items())},
        "transitions": dict(sorted(pairs.items())),
        "agreement": same / decided if decided else math.nan,
        "rejudged_code": sorted(rejudged),
        "discordant": discordant,
    }


def _verdict_key(verdict: dict[str, Any]) -> str:
    status = verdict.get("status", "missing")
    if status != "survived":
        return status
    witness = verdict.get("witness")
    return (
        "survived: unknown"
        if witness is None and "fix_total" not in verdict and verdict.get("reason")
        else "survived: witness"
        if witness
        else "survived: not a witness"
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rate(results: list[BugResult], tool: str, measure: str = PRIMARY) -> float:
    return sum(1 for r in results if _detects(r, tool, measure)) / len(results) if results else math.nan


def _median(values: list[int]) -> float:
    values = sorted(values)
    if not values:
        return math.nan
    mid = len(values) // 2
    return float(values[mid]) if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def figures(agg: dict[str, Any], images: Path, focus: str = "") -> list[str]:
    images.mkdir(parents=True, exist_ok=True)
    written = []
    tools = agg["tools"]
    n, p = agg["bugs"], len(agg["projects"])

    def ranked(key: str) -> list[svg.Row]:
        rows = [
            svg.Row(t, v["detection"][key]["rate"], *v["detection"][key]["ci"], emphasis=t == focus)
            for t, v in tools.items()
        ]
        return sorted(rows, key=lambda r: r.value, reverse=True)

    specs = [
        (
            "bench-detection",
            "Real bugs with a pre-fix witness",
            f"{n} bugs, {p} projects · a mutant that exists before the fix, which the old tests miss there and in the "
            "fixed program and a test of the fault kills, twice · 95% CI over projects",
            ranked("prefix"),
        ),
        (
            "bench-detection-fixed",
            "Real bugs with a witness of the fixed program",
            f"{n} bugs, {p} projects · two conditions: the old tests miss the mutant of the fixed program, a test of "
            "the fault kills it, twice · 95% CI over projects",
            ranked("fixed"),
        ),
        (
            "bench-budget-r10",
            "Pre-fix witness among 10 random mutants",
            f"mean over {n} bugs · up to 10 in-scope emissions drawn without replacement, unmaterializable ones "
            "included "
            "· 95% CI over projects",
            sorted(
                [svg.Row(t, v["r"][10], *v["r10_ci"], emphasis=t == focus) for t, v in tools.items()],
                key=lambda r: r.value,
                reverse=True,
            ),
        ),
        (
            "bench-budget-s3",
            "Pre-fix witness among 3 random survivors",
            f"mean over {n} bugs · up to 3 of the tool's mutants the reference oracle judged surviving, drawn without "
            "replacement · 95% CI over projects",
            sorted(
                [svg.Row(t, v["s"][3], *v["s3_ci"], emphasis=t == focus) for t, v in tools.items()],
                key=lambda r: r.value,
                reverse=True,
            ),
        ),
    ]
    for name, title, subtitle, rows in specs:
        for theme in svg.THEMES:
            path = images / f"{name}.{theme.name}.svg"
            path.write_text(svg.ranked_bars(title, subtitle, rows, theme), encoding="utf-8")
            written.append(str(path))
    location = [
        svg.Part("code the fix did not touch", "accent"),
        svg.Part("lines the fix rewrote", "second"),
        svg.Part("only lines the fix inserted", "gray"),
    ]
    rows_location = []
    for tool, v in sorted(tools.items(), key=lambda kv: kv[1]["detection"]["fixed"]["rate"], reverse=True):
        best = v["best_class"]
        rows_location.append(
            (tool, [best.get(c, 0) / n for c in ("unchanged", "rewritten", "inserted")], tool == focus)
        )
    for theme in svg.THEMES:
        path = images / f"bench-witness-location.{theme.name}.svg"
        path.write_text(
            svg.stacked_shares(
                "Where the witnesses of the fixed program lie",
                f"share of {n} bugs with a witness of the fixed program, by the best place "
                "of the tool's witnesses relative to the fix",
                rows_location,
                location,
                theme,
            ),
            encoding="utf-8",
        )
        written.append(str(path))
    points = [(t, v["emitted_per_bug_median"], v["detection"][PRIMARY]["rate"], t == focus) for t, v in tools.items()]
    for theme in svg.THEMES:
        path = images / f"bench-volume.{theme.name}.svg"
        path.write_text(
            svg.scatter(
                "Detection grows with the number of mutants",
                "share of bugs with a pre-fix witness against the median number of in-scope emissions per bug",
                points,
                theme,
                x_label="in-scope emissions per bug (median, log scale)",
                y_label="bugs with a pre-fix witness",
            ),
            encoding="utf-8",
        )
        written.append(str(path))
    # Killed mutants are the recessive bulk; the survivors carry the colors.
    parts = [
        svg.Part("killed", "gray_light"),
        svg.Part("survived: witness", "second"),
        svg.Part("survived: other", "third"),
        svg.Part("screened out (invalid, equivalent, no-op) or unresolved", "gray"),
    ]
    rows = []
    for tool, v in sorted(tools.items(), key=lambda kv: kv[1]["unique"], reverse=True):
        st = v["status"]
        rows.append(
            (
                tool,
                [
                    st.get("killed", 0),
                    v["witness_mutants"],
                    st.get("survived", 0) - v["witness_mutants"],
                    st.get("invalid", 0)
                    + st.get("equivalent_tce", 0)
                    + st.get("noop", 0)
                    + st.get("unresolved", 0)
                    + st.get("missing", 0),
                ],
                tool == focus,
            )
        )
    for theme in svg.THEMES:
        path = images / f"bench-accounting.{theme.name}.svg"
        path.write_text(
            svg.stacked_bars(
                "What each tool's in-scope mutants turned out to be",
                "unique in-scope mutants per tool over all bugs, by the reference oracle's first "
                "judgement · witnesses of the fixed program confirmed by the re-judgement",
                rows,
                parts,
                theme,
            ),
            encoding="utf-8",
        )
        written.append(str(path))
    for theme in svg.THEMES:
        path = images / f"bench-overlap.{theme.name}.svg"
        path.write_text(
            svg.matrix(
                "Bugs the row tool detects and the column tool does not",
                "pre-fix witnesses · complementarity of the detected bugs",
                agg["overlap"]["labels"],
                agg["overlap"]["values"],
                theme,
                emphasis=focus,
            ),
            encoding="utf-8",
        )
        written.append(str(path))
    return written


def _pct(value: float | None) -> str:
    return "–" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:.0f}%"


def _pct1(value: float | None) -> str:
    """A share with one decimal, for agreement levels a whole percent would round to 100%."""
    return "–" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:.1f}%"


def _span(pair: list[float] | None) -> str:
    return f"{_pct(pair[0])}–{_pct(pair[1])}" if pair else "–"


def _ranks(pair: list[int]) -> str:
    return f"{pair[0]}" if pair[0] == pair[1] else f"{pair[0]}–{pair[1]}"


def _p(value: float) -> str:
    return "<0.0001" if value <= 1e-4 else f"{value:.3g}"


def tables(agg: dict[str, Any]) -> str:
    """The table twins of the figures, and every published breakdown, as markdown."""
    tools = agg["tools"]
    n = agg["bugs"]
    order = sorted(tools, key=lambda t: (-tools[t]["detection"][PRIMARY]["rate"], t))
    out = [
        "### At a glance",
        "",
        "Rank: the 95% interval of the tool's rank by pre-fix detection over the project bootstrap (ties share "
        "the mean rank). Pre-fix detection: bugs with a witness that meets the three conditions. Fixed program: "
        "the two-condition witness (comparable with Just et al. 2014). R(10), S(3): pre-fix witnesses within 10 "
        "random emissions or 3 random survivors. Emissions: the median number of in-scope emissions per bug.",
        "",
        "| tool | rank | pre-fix detection (95% CI) | fixed program | R(10) | S(3) | emissions / bug |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        d = v["detection"]
        out.append(
            f"| {t} | {_ranks(d[PRIMARY]['rank'])} | {_pct(d[PRIMARY]['rate'])} ({_span(d[PRIMARY]['ci'])}) "
            f"| {_pct(d['fixed']['rate'])} | {_pct(v['r'][10])} | {_pct(v['s'][3])} "
            f"| {v['emitted_per_bug_median']:g} |"
        )
    out.append(
        f"| coverage only | {_ranks(agg['coverage_guided_rank'])} | {_pct(agg['coverage_guided_rate'])} "
        f"({_span(agg['coverage_guided_ci'])}) | – | – | – | – |"
    )
    out += [
        "",
        "Coverage only: the bugs whose fail-to-pass tests execute a line of the scope that no context of the "
        "historical suite executed (line coverage alone points at the gap; retrospective, every fail-to-pass test "
        "counts, feature tests too). Its rank is the interval it would take among the tools.",
    ]
    out += [
        "",
        "### Detection",
        "",
        "Operational rates: a bug on which a tool is unavailable (its generator failed or its in-scope emissions "
        "mostly have no faithful source text) counts as not detected; the conditional rate is over the bugs the "
        "tool is available on. Intervals: 95% percentile bootstrap over projects, pointwise; BCa beside it as a "
        "sensitivity check.",
        "",
        "| tool | pre-fix bugs | rate (95% CI) | BCa | project mean | leave one project out | without arrow "
        "| conditional rate (bugs) | unavailable |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        d = v["detection"][PRIMARY]
        out.append(
            f"| {t} | {d['bugs']}/{v['bugs']} | {_pct(d['rate'])} ({_span(d['ci'])}) | {_span(v['rate_ci_bca'])} "
            f"| {_pct(v['rate_project_mean'])} | {_span(v['rate_leave_one_project_out'])} "
            f"| {_pct(v['rate_without_arrow'])} | {_pct(v['rate_conditional'])} ({v['available_bugs']}) "
            f"| {v['unavailable_bugs']} |"
        )
    out += [
        "",
        "### Detection by witness location",
        "",
        "Where the witnesses of the fixed program lie relative to the fix (lines whose stripped text a mutant "
        "changes, against the fix's `-U0` hunks). Fixed program: the two conditions. Location existed: a "
        "witness on a line the fix rewrote or did not touch (an upper approximation of the third condition); "
        "untouched code: a witness the fix did not touch (a lower one); executed: such a witness on a line a "
        "historical test executes. Only inserted lines: every witness on lines the fix added, which no tool could "
        "show before the fix (share of the fixed-program detections). Pre-fix: the exact third condition, "
        "judged on the buggy parent.",
        "",
        "| tool | fixed program | location existed | untouched code | untouched, executed | only inserted lines "
        "| pre-fix | pre-fix rank |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        d = v["detection"]
        inserted = v["best_class"].get("inserted", 0)
        out.append(
            f"| {t} | {d['fixed']['bugs']} ({_pct1(d['fixed']['rate'])}) | {d['existing']['bugs']} "
            f"({_pct1(d['existing']['rate'])}) | {d['unchanged']['bugs']} ({_pct1(d['unchanged']['rate'])}) "
            f"| {d['unchanged_executed']['bugs']} ({_pct1(d['unchanged_executed']['rate'])}) | {inserted} "
            f"({_pct(inserted / d['fixed']['bugs'] if d['fixed']['bugs'] else math.nan)}) "
            f"| {d[PRIMARY]['bugs']} ({_pct1(d[PRIMARY]['rate'])}) | {_ranks(d[PRIMARY]['rank'])} |"
        )
    union = agg["union_rate"]
    out.append(
        f"| all tools | {_pct1(union['fixed'])} | {_pct1(union['existing'])} | {_pct1(union['unchanged'])} "
        f"| {_pct1(union['unchanged_executed'])} | – | {_pct1(union[PRIMARY])} | – |"
    )
    out += [
        "",
        "### Metric reliability",
        "",
        "For each candidate headline measure: the pairs of tools (of all pairs) whose paired 95% interval excludes "
        "zero, the median width of those intervals, and the median Spearman correlation of the tools' order "
        "between two random halves of the projects (2,000 splits).",
        "",
        "| measure | separable pairs | median paired CI width | split-half Spearman |",
        "|---|---|---|---|",
    ]
    names = {
        "prefix": "pre-fix detection",
        "fixed": "fixed-program detection",
        "existing": "location existed",
        "unchanged": "untouched code",
        "unchanged_executed": "untouched code, executed",
        "r10": "R(10), pre-fix",
        "s3": "S(3), pre-fix",
        "r1": "R(1), pre-fix",
        "s1": "S(1), pre-fix",
    }
    for key, value in agg["reliability"].items():
        out.append(
            f"| {names[key]} | {value['separable_pairs']}/{value['pairs']} | "
            f"{value['median_pair_ci_width'] * 100:.1f} pp | {value['split_half_spearman']:.2f} |"
        )
    out += [
        "",
        "### Rank intervals",
        "",
        "95% intervals of each tool's rank over the project bootstrap, per measure (1 = highest).",
        "",
        "| tool | pre-fix | fixed program | location existed | untouched code | R(10) | S(3) |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in order:
        d = tools[t]["detection"]
        out.append(
            f"| {t} | {_ranks(d[PRIMARY]['rank'])} | {_ranks(d['fixed']['rank'])} | {_ranks(d['existing']['rank'])} "
            f"| {_ranks(d['unchanged']['rank'])} | {_ranks(tools[t]['ranks']['r10'])} "
            f"| {_ranks(tools[t]['ranks']['s3'])} |"
        )
    out += [
        "",
        "### Pairs (pre-fix detection)",
        "",
        f"Every pair of tools over all {n} bugs: bugs only the first or only the second detects, the difference "
        "of their rates with its 95% project-bootstrap interval, the two-sided bootstrap p-value and Holm's "
        f"adjustment over all {len(agg['pairs'])} pairs. Minimum detectable difference (half the median interval "
        f"width): ±{agg['statistics']['minimum_detectable_difference'] * 100:.1f} points.",
        "",
        "| first | second | first only | second only | difference (95% CI) | p | Holm |",
        "|---|---|---|---|---|---|---|",
    ]
    for pair in sorted(agg["pairs"], key=lambda x: (order.index(x["first"]), order.index(x["second"]))):
        first, second = (
            (pair["first"], pair["second"])
            if order.index(pair["first"]) < order.index(pair["second"])
            else (pair["second"], pair["first"])
        )
        sign = 1 if first == pair["first"] else -1
        lo, hi = sorted((sign * pair["ci"][0], sign * pair["ci"][1]))
        only_first, only_second = (
            (pair["first_only"], pair["second_only"]) if sign == 1 else (pair["second_only"], pair["first_only"])
        )
        out.append(
            f"| {first} | {second} | {only_first} | {only_second} | {sign * pair['difference'] * 100:+.1f} pp "
            f"({lo * 100:+.1f} to {hi * 100:+.1f}) | {_p(pair['p'])} | {_p(pair['p_holm'])} |"
        )
    out += [
        "",
        "### Budgets",
        "",
        "R(q): q of the tool's in-scope emissions drawn without replacement (unmaterializable ones included). "
        "S(k): k of the unique artifacts of the tool that the reference oracle judges survivors (the tool's own "
        "engine may report others: S(k) rates the generator under a faithful engine). Both use "
        "min(budget, population) draws and are 0 for an empty population; witnesses are pre-fix ones unless "
        "marked fixed program. S(3) executed: only survivors on lines the historical suite executes (a test or "
        "the import); S(3) by line: lines with a survivor as the unit, a line with a witness as the hit. R(1) "
        "and S(1) are omitted: their tool order does not repeat between halves of the projects.",
        "",
        "| tool | R(10) (95% CI) | R(100) | S(3) (95% CI) | S(10) | S(3) executed | S(3) by line "
        "| R(10) fixed program | S(3) fixed program | emissions / bug | survivors / bug |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        out.append(
            f"| {t} | {_pct(v['r'][10])} ({_span(v['r10_ci'])}) | {_pct(v['r'][100])} "
            f"| {_pct(v['s'][3])} ({_span(v['s3_ci'])}) | {_pct(v['s'][10])} | {_pct(v['s_executed'][3])} "
            f"| {_pct(v['s_lines'][3])} | {_pct(v['r_fixed'][10])} | {_pct(v['s_fixed'][3])} "
            f"| {v['emitted_per_bug_median']:g} | {v['survivors_per_bug_median']:g} |"
        )
    out += [
        "",
        "### Witnesses and coverage",
        "",
        "What the historical suite executes on the lines of each witness of the fixed program: a test (`test`), "
        "only the import of the module or the class body (`import`), or nothing (`never`); then the bugs by the "
        'best of the tool\'s witnesses. Engines that report a mutant on a line no test executes as "no coverage" '
        "show the user no survivor there.",
        "",
        "| tool | witnesses: test / import / never | bugs: best witness under a test | on an import line | only on "
        "never-executed lines (share of fixed-program detections) | pre-fix under a test |",
        "|---|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        run = v["witness_execution"]
        total = sum(run.values()) or 1
        best = v["best_execution"]
        detected = v["detection"]["fixed"]["bugs"]
        out.append(
            f"| {t} | {_pct(run.get('test', 0) / total)} / {_pct(run.get('import', 0) / total)} / "
            f"{_pct(run.get('never', 0) / total)} | {best.get('test', 0)} | {best.get('import', 0)} "
            f"| {best.get('never', 0)} ({_pct(best.get('never', 0) / detected if detected else math.nan)}) "
            f"| {v['detection']['prefix_tested']['bugs']} ({_pct1(v['detection']['prefix_tested']['rate'])}) |"
        )
    out += [
        "",
        "### Mutant accounting (unique in-scope mutants, bugs the tool is available on)",
        "",
        "Unmaterializable: in-scope emissions for which the tool's adapter found no faithful source text; they "
        "count in R(q) and are never judged.",
        "",
        "| tool | in-scope emissions | unmaterializable | unique | killed | survived | of which witnesses "
        "| of which pre-fix | invalid | equivalent (TCE) | no-op | unresolved |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in sorted(order, key=lambda t: tools[t]["unique"], reverse=True):
        v, st = tools[t], tools[t]["status"]
        out.append(
            f"| {t} | {v['emitted']} | {v['unmaterializable']} | {v['unique']} | {st.get('killed', 0)} "
            f"| {st.get('survived', 0)} | {v['witness_mutants']} | {v['prefix_witness_mutants']} "
            f"| {st.get('invalid', 0)} | {st.get('equivalent_tce', 0)} | {st.get('noop', 0)} "
            f"| {st.get('unresolved', 0) + st.get('missing', 0)} |"
        )
    out += [
        "",
        "### Witness flow (unique artifacts)",
        "",
        "A first-judgement witness counts when the re-judgement from fresh trees reproduces it (re-judgements "
        "without an outcome are unknown, the others not reproduced), the same fail-to-pass test is red with an "
        "outcome of its own in both judgements (red only through a collection or startup failure of the whole run "
        "is synthetic) and that test checks the fault (otherwise feature only). Pre-fix: of those, the witnesses "
        "whose counterpart in the buggy parent survives the historical suite; undecided: the counterpart's "
        "verdict is missing or unresolved, or no faithful counterpart could be written.",
        "",
        "| tool | first-judgement witnesses | re-judgement unknown | not reproduced | synthetic | feature only "
        "| witnesses | in untouched / rewritten / inserted lines | pre-fix | pre-fix undecided |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in order:
        f = tools[t]["witness_flow"]
        c = tools[t]["witness_classes"]
        out.append(
            f"| {t} | {f['first_witnesses']} | {f['unknown_confirmations']} | {f['unconfirmed_witnesses']} "
            f"| {f['synthetic_witnesses']} | {f['feature_only_witnesses']} | {f['witnesses']} "
            f"| {c.get('unchanged', 0)} / {c.get('rewritten', 0)} / {c.get('inserted', 0)} "
            f"| {tools[t]['prefix_witness_mutants']} | {tools[t]['prefix_undecided_witnesses']} |"
        )
    out += [
        "",
        "### Undecided outcomes",
        "",
        "Artifacts that could still be pre-fix witnesses: an unresolved historical run, a survivor whose "
        "fail-to-pass run gave no outcome, a witness whose re-judgement gave none, or a witness whose buggy-side "
        "counterpart is undecided. The optimistic rate counts every bug with one of them as detected.",
        "",
        "| tool | undecided artifacts | bugs undetected with an undecided artifact | rate | optimistic rate |",
        "|---|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        out.append(
            f"| {t} | {v['undecided_mutants'] + v['prefix_undecided_witnesses']} "
            f"| {v['undecided_bugs'] + v['prefix_undecided_bugs']} | {_pct(v['detection'][PRIMARY]['rate'])} "
            f"| {_pct(v['rate_optimistic'])} |"
        )
    sizes = agg["strata_sizes"]
    titles = {
        "region": "Where the fix is",
        "coverage": "Coverage of the changed lines (retrospective)",
        "composition": "What the fix's hunks do",
        "size": "Lines the fix inserted or rewrote",
        "period": "When the fix was committed",
        "ai_marker": "An explicit AI-tool marker in the fix commit",
    }
    out += [
        "",
        "### By stratum (pre-fix detection)",
        "",
        "Region: inside functions, in class-body or module-level statements, both. Coverage: the fail-to-pass run "
        "executes a changed line and the historical suite executed every such line (covered), not every one "
        "(gap), or the fail-to-pass run executes none (unrun); every fail-to-pass test counts, feature tests too. "
        "Composition: every hunk only adds lines (insertion), every hunk removes lines (rewrite), or both. AI "
        "marker: a `Co-authored-by` or `Assisted-by` trailer, a generated-with line or a merged agent branch "
        "naming an AI tool.",
    ]
    for name, values in sizes.items():
        out += [
            "",
            f"{titles[name]}: " + ", ".join(f"{value} ({count} bugs)" for value, count in values.items()),
            "",
            "| tool | " + " | ".join(values) + " |",
            "|---|" + "---|" * len(values),
        ]
        for t in order:
            out.append(f"| {t} | " + " | ".join(_pct(tools[t]["strata"][name][value]) for value in values) + " |")
    out += [
        "",
        "### Secondary measures",
        "",
        "Only this tool: bugs no other tool detects (pre-fix). Re-creates the fault: single-file fixes where a "
        "mutant whose edit touches a line the fix changed turns the fixed file into the buggy one (equal ASTs). "
        "Witness by any fail-to-pass test: the fixed-program witness without the purity filter.",
        "",
        "| tool | only this tool | re-creates the fault (applicable bugs) | witness by any fail-to-pass test |",
        "|---|---|---|---|",
    ]
    for t in order:
        v = tools[t]
        out.append(
            f"| {t} | {v['only_this_tool']} | {v['exact_fault']} ({v['exact_fault_applicable']}) "
            f"| {_pct(v['rate_any_fix_test'])} |"
        )
    noise = agg["noise"]
    out += [
        "",
        f"Bugs: {n} in {len(agg['projects'])} projects; detected by at least one tool: "
        f"{_pct(agg['union_rate'][PRIMARY])} (pre-fix), {_pct(agg['union_rate']['fixed'])} (fixed program). "
        f"Scope regions left out as holding no change of the program: "
        f"{', '.join(f'{v} {k}' for k, v in noise['regions'].items()) or 'none'} in {noise['bugs']} bugs "
        "(`excluded_regions.json`).",
    ]
    corpus = agg["corpus"]
    out += [
        "",
        "### Corpus",
        "",
        "Fix commits by year (committer date): "
        + ", ".join(f"{y} {c}" for y, c in corpus["years"].items())
        + f". With an explicit AI-tool marker: {corpus['ai_marker_bugs']} ("
        + ", ".join(f"{p} {c}" for p, c in corpus["ai_marker_by_project"].items())
        + "). Fix composition: "
        + ", ".join(f"{k} {v}" for k, v in corpus["composition"].items())
        + f"; median lines inserted or rewritten: {corpus['changed_lines_median']:g}.",
    ]
    out += _sample_tables(agg)
    out += _cost_table(agg)
    out += _engine_side_by_side(agg)
    return "\n".join(out) + "\n"


def _sample_tables(agg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    sens = agg.get("oracle_sensitivity", {})
    if sens.get("sampled"):
        full = sens["full_suite"]
        killed, survived = full.get("killed", 0), full.get("survived", 0)
        out += [
            "",
            "### Oracle shortcuts",
            "",
            f"{sens['sampled']} kills by a single test or the covering subset, drawn uniformly without replacement "
            f"(seed {sens['seed']}) from {sens['population']} over {sens['bugs']} bugs, judged again by the whole "
            f"historical suite in canonical order alone: killed {killed}, survived {survived}, other "
            f"{sens['sampled'] - killed - survived} ({json.dumps(full, sort_keys=True)}). Killed of the decided "
            f"outcomes: {_pct1(sens['agreement'])} ({killed}/{killed + survived}); of the sample: "
            f"{_pct1(sens['killed_of_sampled'])} ({killed}/{sens['sampled']}). By the first judgement's stage: "
            f"{json.dumps(sens['by_first_stage'], sort_keys=True)}.",
        ]
        if sens["discordant"]:
            out += [
                "",
                "| bug | artifact | first stage | first killed by | whole suite | fault tests red | tools |",
                "|---|---|---|---|---|---|---|",
            ]
            for d in sens["discordant"]:
                out.append(
                    f"| {d['bug']} | {d['aid'][:12]} | {d['first_stage']} | {', '.join(d['first_killed_by'])} "
                    f"| {d['full_suite']}{f' ({d["reason"]})' if d['reason'] else ''} "
                    f"| {', '.join(d['fault_tests_red']) or '–'} | {', '.join(d['tools'])} |"
                )
    eq = agg.get("oracle_equivalence", {})
    if eq.get("sampled"):
        out += [
            "",
            "### Oracle code equivalence",
            "",
            f"{eq['sampled']} judged artifacts drawn uniformly without replacement (seed {eq['seed']}) from "
            f"{eq['population']} over {eq['bugs']} bugs, judged again in bug mode by oracle code "
            f"{', '.join(eq['rejudged_code'])}; by the code that recorded the first verdict: "
            f"{json.dumps(eq['outcome_by_recorded_code'], sort_keys=True)}. The same verdict for "
            f"{_pct1(eq['agreement'])} of the decided pairs; a survivor's verdict includes its witness outcome.",
        ]
        if eq["discordant"]:
            out += [
                "",
                "| bug | artifact | recorded | re-judged | recorded killed by | re-judged killed by |",
                "|---|---|---|---|---|---|",
            ]
            for d in eq["discordant"]:
                out.append(
                    f"| {d['bug']} | {d['aid'][:12]} | {d['recorded']} | {d['rejudged']} "
                    f"| {', '.join(d['recorded_killed_by']) or '–'} | {', '.join(d['rejudged_killed_by']) or '–'} |"
                )
    return out


def _engine_side_by_side(agg: dict[str, Any]) -> list[str]:
    engine = agg.get("engine")
    if not engine:
        return []
    out = [
        "",
        "### Generator and engine side by side",
        "",
        "S(k) rates the generator under a faithful engine: the survivors it draws are the reference oracle's. A "
        "tool's own engine shows its user its own survivors; the engine track measures how many of them the oracle "
        "kills (false survivors) on its six projects, and on how many of them the campaign gave no verdict.",
        "",
        "| tool | pre-fix detection | location existed | S(3) | engine false survivors | campaigns without verdicts |",
        "|---|---|---|---|---|---|",
    ]
    tools = agg["tools"]
    for tool in sorted(tools, key=lambda t: (-tools[t]["detection"][PRIMARY]["rate"], t)):
        e = engine.get(tool, {})
        false_survivors = e.get("false_survivor")
        shown = "n/a" if false_survivors is None or math.isnan(false_survivors) else f"{false_survivors * 100:.1f}%"
        out.append(
            f"| {tool} | {_pct(tools[tool]['detection'][PRIMARY]['rate'])} "
            f"| {_pct(tools[tool]['detection']['existing']['rate'])} | {_pct(tools[tool]['s'][3])} | {shown} "
            f"| {e.get('not_run', 'n/a')} of {e.get('planned', 'n/a')} |"
        )
    return out


def engine_summary(path: Path) -> dict[str, Any]:
    """Per tool from the published engine track (``engine_track.json``): the share of its survivors the oracle
    kills and its campaigns without verdicts."""
    from pymutbench.report_engine import engine_totals

    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    totals, _ = engine_totals(report)
    return {
        tool: {
            "false_survivor": total["false_survivor"],
            "planned": len(report),
            "not_run": len(report) - len(total["ran"]),
        }
        for tool, total in totals.items()
    }


def _cost_table(agg: dict[str, Any]) -> list[str]:
    cost = agg.get("cost")
    if not cost:
        return []
    lite = cost["lite"]
    out = [
        "",
        "### Cost and the Lite subset",
        "",
        "Judging cost: the oracle's recorded seconds of every artifact of the current unions (first judgement), in "
        "worker-hours; a worker is one pytest process at a time. Lite: per project up to "
        f"{LITE_PER_PROJECT} bugs drawn with seed {lite['seed']}, projects whose median historical suite runs longer "
        f"than {LITE_SUITE_LIMIT:.0f} s left out ({', '.join(lite['projects_left_out'])}): {lite['bugs']} bugs, "
        f"{lite['worker_hours']:.1f} worker-hours. Against the full corpus (pre-fix detection): Spearman "
        f"{lite['spearman']:.3f}, largest rate difference {lite['max_difference'] * 100:.1f} points; over "
        f"{lite['draws']} draws by the same rule, Spearman median {lite['spearman_median']:.3f} (5th percentile "
        f"{lite['spearman_p5']:.3f}), largest difference median {lite['max_difference_median'] * 100:.1f} points "
        f"(95th percentile {lite['max_difference_p95'] * 100:.1f}). The Lite bug ids are in `lite.json`.",
        "",
        "| project | bugs | judging worker-hours | per bug (median) | historical suite (median s) | in Lite |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in cost["projects"].items():
        out.append(
            f"| {name} | {row['bugs']} | {row['worker_hours']:.1f} | {row['per_bug_median_hours']:.2f} "
            f"| {row['suite_seconds_median']:.0f} | {row['lite']} |"
        )
    out.append(f"| **total** | **{cost['bugs']}** | **{cost['worker_hours']:.1f}** | | | **{lite['bugs']}** |")
    out += [
        "",
        "What judging a tool alone would add: the artifacts only that tool emitted (no other tool has the same "
        "mutated file) and their judging cost; a new tool pays for its artifacts no published tool has.",
        "",
        "| tool | artifacts only this tool emitted | worker-hours |",
        "|---|---|---|",
    ]
    for tool, row in sorted(cost["tool_only"].items(), key=lambda kv: kv[1]["worker_hours"]):
        out.append(f"| {tool} | {row['artifacts']} | {row['worker_hours']:.1f} |")
    return out


def cards(agg: dict[str, Any], out: Path) -> None:
    """One card per tool (``results/cards/<tool>.json``), with everything a published result must name: the corpus
    and results versions, the tool's version and configuration, the oracle semantics, the measures with intervals
    and ranks, and the paired differences from every other tool."""
    out.mkdir(parents=True, exist_ok=True)
    for tool, row in agg["tools"].items():
        entry = REGISTRY.get(tool)
        pairs = [
            {
                "other": p["second"] if p["first"] == tool else p["first"],
                "difference": p["difference"] if p["first"] == tool else -p["difference"],
                "ci": p["ci"] if p["first"] == tool else [-p["ci"][1], -p["ci"][0]],
                "p": p["p"],
                "p_holm": p["p_holm"],
            }
            for p in agg["pairs"]
            if tool in (p["first"], p["second"])
        ]
        card = {
            "tool": tool,
            "version": entry.version if entry else None,
            "configuration": "default"
            + (
                f" (emissions with native ids starting {entry.default_drop!r} left out)"
                if entry and entry.default_drop
                else ""
            ),
            **agg["release"],
            "oracle": agg["oracle"],
            "bugs": agg["bugs"],
            "available_bugs": row["available_bugs"],
            "detection": row["detection"],
            "r10": row["r"][10],
            "r10_ci": row["r10_ci"],
            "r10_rank": row["ranks"]["r10"],
            "s3": row["s"][3],
            "s3_ci": row["s3_ci"],
            "s3_rank": row["ranks"]["s3"],
            "emitted_per_bug_median": row["emitted_per_bug_median"],
            "pairs": pairs,
            "primary": agg["primary"],
            "statistics": agg["statistics"],
        }
        (out / f"{tool}.json").write_text(json.dumps(card, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def cost_and_lite(results: list[BugResult]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The judging cost per project and the Lite subset: a deterministic, seeded, project-stratified draw that
    leaves out the slowest projects, with its agreement with the full corpus over many draws by the same rule."""
    seconds: dict[str, float] = {}
    suite: dict[str, list[float]] = {}
    own: dict[str, list[float]] = {tool: [0.0, 0.0] for tool in TOOLS}  # artifacts only that tool emitted: count, s
    for result in results:
        directory = OUT / "gen" / result.subject / result.commit[:12]
        union = {
            row["aid"]: {o.get("tool") for o in row.get("origins", [])} for row in _rows(directory / "union.jsonl")
        }
        latest = {row["aid"]: row for row in _rows(directory / "oracle.jsonl") if "aid" in row}
        cid = f"{result.subject}@{result.commit[:12]}"
        seconds[cid] = sum(float(latest[aid].get("seconds", 0.0)) for aid in union if aid in latest)
        for aid, origins in union.items():
            if len(origins) == 1 and aid in latest and (tool := next(iter(origins))) in own:
                own[tool][0] += 1
                own[tool][1] += float(latest[aid].get("seconds", 0.0))
        bug = json.loads((directory / "bug.json").read_text(encoding="utf-8"))
        suite.setdefault(result.subject, []).append(float(bug.get("old_suite_seconds", 0.0)))
    slow = sorted(p for p, values in suite.items() if median(values) > LITE_SUITE_LIMIT)
    by_project: dict[str, list[BugResult]] = {}
    for result in sorted(results, key=lambda r: (r.subject, r.commit)):
        by_project.setdefault(result.subject, []).append(result)

    def draw(seed: int) -> list[BugResult]:
        rng = random.Random(seed)
        chosen = []
        for project, bugs in sorted(by_project.items()):
            if project not in slow:
                chosen += rng.sample(bugs, min(LITE_PER_PROJECT, len(bugs)))
        return chosen

    full = {t: _rate(results, t) for t in TOOLS}

    def agreement(subset: list[BugResult]) -> tuple[float, float]:
        rates = {t: _rate(subset, t) for t in TOOLS}
        return (
            spearman([full[t] for t in TOOLS], [rates[t] for t in TOOLS]),
            max(abs(rates[t] - full[t]) for t in TOOLS),
        )

    lite = draw(SEED)
    rho, gap = agreement(lite)
    draws = [agreement(draw(SEED + i)) for i in range(1, 1001)]
    rhos, gaps = sorted(d[0] for d in draws), sorted(d[1] for d in draws)
    lite_ids = sorted(f"{r.subject}@{r.commit[:12]}" for r in lite)
    projects = {}
    for project, bugs in sorted(by_project.items()):
        hours = [seconds[f"{r.subject}@{r.commit[:12]}"] / 3600 for r in bugs]
        projects[project] = {
            "bugs": len(bugs),
            "worker_hours": sum(hours),
            "per_bug_median_hours": median(hours),
            "suite_seconds_median": median(suite[project]),
            "lite": sum(1 for r in lite if r.subject == project),
        }
    summary = {
        "seed": SEED,
        "bugs": len(lite),
        "projects_left_out": slow,
        "worker_hours": sum(seconds[i] for i in lite_ids) / 3600,
        "spearman": rho,
        "max_difference": gap,
        "draws": len(draws),
        "spearman_median": median(rhos),
        "spearman_p5": rhos[len(rhos) // 20],
        "max_difference_median": median(gaps),
        "max_difference_p95": gaps[len(gaps) * 19 // 20],
    }
    cost = {
        "bugs": len(results),
        "worker_hours": sum(seconds.values()) / 3600,
        "projects": projects,
        "lite": summary,
        "tool_only": {
            tool: {"artifacts": int(count), "worker_hours": secs / 3600} for tool, (count, secs) in sorted(own.items())
        },
    }
    return cost, {
        "rule": {"per_project": LITE_PER_PROJECT, "suite_limit_seconds": LITE_SUITE_LIMIT, "seed": SEED},
        "bug_ids": lite_ids,
        **{k: v for k, v in summary.items() if k != "seed"},
    }


def corpus_table(results: list[BugResult]) -> str:
    """The selection funnel per project (``bugs/selection.json``) with the bugs the oracle judged."""
    selection = json.loads((OUT / "bugs" / "selection.json").read_text(encoding="utf-8"))
    judged = Counter(r.subject for r in results)
    keys = ("candidates", "screened", "admitted", "adjudicated", "bugs", "readmitted", "selected")
    out = [
        "| project | candidates | validated | admitted | adjudicated | labelled bug | re-admitted | selected "
        "| judged | selection |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    totals: Counter[str] = Counter()
    for name, row in sorted(selection["subjects"].items()):
        totals.update({k: row[k] for k in keys})
        totals["judged"] += judged.get(name, 0)
        state = "complete" if row["complete"] else f"{len(row['open'])} open"
        out.append(f"| {name} | " + " | ".join(str(row[k]) for k in keys) + f" | {judged.get(name, 0)} | {state} |")
    out.append("| **total** | " + " | ".join(f"**{totals[k]}**" for k in (*keys, "judged")) + " | |")
    return "\n".join(out) + "\n"


def _write_gz(path: Path, lines: list[str]) -> None:
    """A gzip file whose bytes depend on its content only (no name, no time in the header)."""
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz,
        io.TextIOWrapper(gz, encoding="utf-8") as fh,
    ):
        fh.writelines(lines)


DATA_FILES: dict[str, str] = {  # published data file -> the per-bug file it collects
    "artifacts": "union.jsonl",
    "verdicts": "oracle.jsonl",
    "confirmations": "oracle.confirm.jsonl",
    "full_suite": "oracle.full.jsonl",
    "equivalence": "oracle.equiv.jsonl",
    "parent_artifacts": "parent.union.jsonl",
    "parent_verdicts": "oracle.parent.jsonl",
}


def export(results: list[BugResult], out: Path) -> list[str]:
    """The judged corpus as compressed JSON lines, one record per bug and item, for reuse without the harness:
    every artifact with its tools' origins, every oracle verdict and re-judgement, the buggy-side counterparts
    and their verdicts, every generation ledger, every bug's scope, its fix record and its witness map."""
    written = []
    for name, filename in DATA_FILES.items():
        lines = []
        for result in results:
            directory = OUT / "gen" / result.subject / result.commit[:12]
            for row in _rows(directory / filename):
                # A meta record names its bug by the full commit: kept as ``commit`` under the common key.
                fields = {("commit" if key == "bug" else key): value for key, value in row.items()}
                lines.append(
                    json.dumps({"bug": f"{result.subject}@{result.commit[:12]}", **fields}, sort_keys=True) + "\n"
                )
        _write_gz(out / f"{name}.jsonl.gz", lines)
        written.append(str(out / f"{name}.jsonl.gz"))
    lines = []
    for result in results:
        directory = OUT / "gen" / result.subject / result.commit[:12]
        bug = f"{result.subject}@{result.commit[:12]}"
        lines.append(
            json.dumps(
                {
                    "bug": bug,
                    "scope": json.loads((directory / "scope.json").read_text(encoding="utf-8")),
                    "record": json.loads((directory / "bug.json").read_text(encoding="utf-8")),
                    "fix": fixes.load(directory),
                    "prefix": json.loads((directory / "prefix.json").read_text(encoding="utf-8"))
                    if (directory / "prefix.json").exists()
                    else None,
                },
                sort_keys=True,
            )
            + "\n"
        )
        for side, folder in (("fixed", directory), ("buggy", directory / "buggy")):
            for ledger in sorted(folder.glob("*.ledger.json")):
                lines.append(
                    json.dumps(
                        {
                            "bug": bug,
                            "side": side,
                            "tool": ledger.name.removesuffix(".ledger.json"),
                            "ledger": json.loads(ledger.read_text(encoding="utf-8")),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    _write_gz(out / "generation.jsonl.gz", lines)
    written.append(str(out / "generation.jsonl.gz"))
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bugs")
    p.add_argument("--results", required=True)
    p.add_argument("--images", required=True)
    p.add_argument("--focus", default="", choices=("", *TOOLS), help="highlight one tool in the figures")
    p.add_argument(
        "--accept-incomplete",
        default="",
        help="comma-separated subjects whose incomplete selection is published as such",
    )
    p.add_argument("--preview", action="store_true", help="report the publication gaps instead of refusing")
    p.add_argument(
        "--engine-track",
        default=str(BENCH / "results" / "engine_track.json"),
        help="the engine track's published results (report_engine engine), summarized beside S(k)",
    )
    args = parser.parse_args()
    accepted = frozenset(s for s in args.accept_incomplete.split(",") if s)
    results = load(accept_incomplete=accepted, preview=args.preview)
    agg = aggregate(results)
    agg["oracle_sensitivity"] = sensitivity()
    agg["oracle_equivalence"] = equivalence()
    agg["cost"], lite = cost_and_lite(results)
    agg["engine"] = engine_summary(Path(args.engine_track))
    agg["selection_incomplete_accepted"] = sorted(accepted)
    agg["preview_problems"] = list(PROBLEMS) if args.preview else []
    out = Path(args.results)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bug_track.json").write_text(
        json.dumps(agg, indent=1, sort_keys=True, default=float) + "\n", encoding="utf-8"
    )
    (out / "bugs.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in to_json(results)), encoding="utf-8"
    )
    written = figures(agg, Path(args.images), args.focus)
    (out / "tables.md").write_text(tables(agg), encoding="utf-8")
    (out / "corpus.md").write_text(corpus_table(results), encoding="utf-8")
    (out / "lite.json").write_text(json.dumps(lite, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cards(agg, out / "cards")
    (out / "oracle_equivalence.json").write_text(
        json.dumps(agg["oracle_equivalence"], indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "excluded_regions.json").write_text(
        json.dumps(
            [
                {"bug": f"{r.subject}@{r.commit[:12]}", "path": path, "lines": [start, end], "reason": reason}
                for r in results
                for path, start, end, reason in r.noise_regions
            ],
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    # The corpus decisions behind the numbers: the selection funnel, every adjudication and the raters' agreement,
    # the purity rubric with the labels of the selected bugs, and the oracle samples.
    for source in (
        OUT / "bugs" / "selection.json",
        OUT / "review" / "final.jsonl",
        OUT / "review" / "agreement.json",
        OUT / "review" / "purity_rubric.md",
        OUT / "review" / "purity_schema.json",
        OUT / "bugs" / "sensitivity.json",
        OUT / "bugs" / "equivalence.json",
    ):
        if source.exists():
            (out / source.name).write_bytes(source.read_bytes())
    selected = {f"{r.subject}@{r.commit[:12]}" for r in results} | {e["id"] for e in EXCLUDED}
    labels = [
        item
        for path in sorted((OUT / "review" / "purity").glob("batch-*.json"))
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", [])
        if item["id"] in selected
    ]
    (out / "purity.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in sorted(labels, key=lambda i: i["id"])),
        encoding="utf-8",
    )
    (out / "excluded.json").write_text(json.dumps(EXCLUDED, indent=1) + "\n", encoding="utf-8")
    (out / "unavailable.json").write_text(json.dumps(UNAVAILABLE, indent=1) + "\n", encoding="utf-8")
    data = out / "data"
    data.mkdir(exist_ok=True)
    export(results, data)
    print(f"{agg['bugs']} bugs; wrote {len(written)} figures")
    for tool, v in sorted(agg["tools"].items(), key=lambda kv: kv[1]["detection"][PRIMARY]["rate"], reverse=True):
        d = v["detection"]
        print(
            f"{tool:<17} pre-fix {d[PRIMARY]['bugs']:>3}/{v['bugs']} = {d[PRIMARY]['rate']:.1%} "
            f"[{d[PRIMARY]['ci'][0]:.1%}, {d[PRIMARY]['ci'][1]:.1%}] rank {_ranks(d[PRIMARY]['rank'])}  "
            f"fixed {d['fixed']['rate']:.1%}  R10={v['r'][10]:.2f}  S3={v['s'][3]:.2f}"
        )


if __name__ == "__main__":
    main()
