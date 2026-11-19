"""The engine and conformance tracks: native verdicts against the reference oracle, cost, stability, traps.

    python3 -m pymutbench.report_engine engine --results bench/results --images docs/images
    python3 -m pymutbench.report_engine conformance --results bench/results --images docs/images

A native verdict is compared with the reference oracle's verdict of the identical mutated file (the artifact both
map to; the oracle's staged procedure, without the fail-to-pass step):

* ``agree: killed``, ``agree: survived`` — the tool and the oracle give the same verdict;
* ``unconfirmed kill`` — the tool says killed, the reference oracle does not kill it;
* ``missed kill`` — the tool says survived, the reference oracle kills it;
* ``no coverage, killable`` — the tool ran no test, the oracle kills it; ``no coverage`` with an oracle survivor
  agrees;
* ``timeout``, ``error`` and ``skipped`` are the tool's own non-verdicts, counted apart; ``unmapped`` native
  mutants have no artifact (no verified source edit reproduces the tool's mutant), ``unrepresentable`` ones need
  the tool's runtime support, ``no reference verdict`` ones have an artifact the oracle has not judged,
  ``oracle <status>`` ones an artifact the oracle found invalid, a no-op, equivalent, gated out or unresolved, and
  ``<verdict>, count only`` ones a verdict the tool reports only as a total (deployproof's runner errors, and its
  kills unless its report reconciles with the adapter's enumeration).

The shares of false kills and false survivors are over the comparable verdicts only (the agreeing ones and the
disagreeing ones); every other category is listed beside them. The planned campaigns are every tool of
``pipeline.NATIVE_TOOLS`` on every project of ``pipeline.ENGINE_SUBJECTS`` (and every conformance fixture). A
campaign that left no result is reported as ``failed`` (``<tool>.r1.failed.json``) with the reason its log
gives, one whose tool gave none of its mutants a verdict (killed, survived, timeout, no coverage) as ``no
verdicts`` with the tool's last exit status and the error its report gives or its last output line, and one never
run refuses the report unless ``--preview``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import svg
from pymutbench.fingerprint import oracle_semantics
from pymutbench.native import VENV
from pymutbench.orchestrate import BENCH, OUT, trap_module
from pymutbench.pipeline import ENGINE_SUBJECTS, NATIVE_TOOLS

KILLED = {"killed"}
SURVIVED = {"survived"}
ENGINE_MEMORY_MIB = 3072  # a campaign container's memory limit (PMB_ENGINE_MEMORY=3g): a peak at it measures a test
VERDICTS = {"killed", "survived", "timeout", "no_coverage"}  # native verdicts; "skipped" and "error" are none


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _count_only(native: dict[str, Any]) -> Counter[str]:
    """The verdicts a tool reports only as counts: deployproof lists its survivors one by one and gives its kills
    and runner errors as totals. Its kills have rows of their own (``killed (by count)``) when its report
    reconciles with the adapter's enumeration; only the kills without a row are counted here."""
    tally: Counter[str] = Counter()
    rows = native.get("mutants", [])
    listed_kills = sum(1 for row in rows if row.get("native_status") == "killed (by count)")
    for row in rows:
        if row.get("native_id") == "<counts>":
            counts = json.loads(row["native_status"])
            tally["killed"] += (counts.get("killed_mutants") or 0) - listed_kills
            tally["error"] += counts.get("runner_errors_count") or 0
    return +tally


def native_verdicts(native: dict[str, Any]) -> dict[str, int]:
    """Every native mutant's verdict, the ones reported only as counts included."""
    verdicts = Counter(r["verdict"] for r in native.get("mutants", []) if r.get("native_id") != "<counts>")
    return dict(verdicts + _count_only(native))


def compare(native: dict[str, Any], oracle: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter({f"{verdict}, count only": n for verdict, n in _count_only(native).items()})
    examples: dict[str, list[str]] = {}
    for row in native.get("mutants", []):
        if row.get("native_id") == "<counts>":
            continue
        if row.get("unrepresentable"):
            counts["unrepresentable"] += 1
            continue
        aid = row.get("aid")
        if not aid:
            counts["unmapped"] += 1
            continue
        if aid not in oracle:
            counts["no reference verdict"] += 1
            continue
        reference = oracle[aid]["status"]
        verdict = row["verdict"]
        if reference in {"invalid", "noop", "equivalent_tce", "unresolved", "gated_out"}:
            key = f"oracle {reference}"
        elif verdict in KILLED:
            key = "agree: killed" if reference == "killed" else "unconfirmed kill"
        elif verdict in SURVIVED:
            key = "agree: survived" if reference == "survived" else "missed kill"
        elif verdict == "no_coverage":
            key = "no coverage, killable" if reference == "killed" else "agree (no coverage)"
        else:
            key = verdict
        counts[key] += 1
        if key in {"unconfirmed kill", "missed kill", "no coverage, killable"}:
            examples.setdefault(key, []).append(aid)
    total = sum(counts.values())
    return {"counts": dict(counts), "total": total, "examples": {k: v[:5] for k, v in examples.items()}}


def _run_verdicts(run: dict[str, Any]) -> dict[tuple[str, ...], frozenset[str]]:
    """A run's verdicts per mutant. A mapped native mutant is identified by its artifact (the mutated file), since a
    tool may number its mutants anew in every run (mutation's ids are time-based); an unmapped one by its file and
    native id. The native mutants of one mutated file keep all their verdicts."""
    found: dict[tuple[str, ...], set[str]] = {}
    for r in run.get("mutants", []):
        key = ("artifact", r["aid"]) if r.get("aid") else ("native", str(r.get("path")), str(r["native_id"]))
        found.setdefault(key, set()).add(r["verdict"])
    return {key: frozenset(verdicts) for key, verdicts in found.items()}


def stability(first: dict[str, Any], second: dict[str, Any]) -> dict[str, int]:
    """A repetition against the first run: the mutants both runs have, how many of them got other verdicts, and the
    mutants only one run has (mutation draws its number literals anew in every run)."""
    a, b = _run_verdicts(first), _run_verdicts(second)
    common = a.keys() & b.keys()
    return {"common": len(common), "changed": sum(1 for k in common if a[k] != b[k]),
            "only_first": len(a.keys() - b.keys()), "only_second": len(b.keys() - a.keys())}


def varied(runs: list[dict[str, Any]]) -> dict[str, int]:
    """The mutants every run of a campaign has whose verdicts differ between the runs, counted by the verdicts they
    got (``killed/timeout``)."""
    keyed = [_run_verdicts(run) for run in runs]
    common = set(keyed[0]).intersection(*keyed[1:])
    found = Counter("/".join(sorted(frozenset().union(*(k[key] for k in keyed)))) for key in common
                    if len({k[key] for k in keyed}) > 1)
    return dict(sorted(found.items()))


def _no_result(state: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """A planned campaign without a result: ``failed`` (it ran and left no report) or ``missing`` (never run)."""
    return {"result": state, "reason": (detail or {}).get("reason"), "detail": detail or {}, "native_mutants": 0,
            "mapped": 0, "wall_seconds": [None],
            "cpu_seconds": [None], "native_verdicts": {}, "agreement": {"counts": {}, "total": 0},
            "stability": [], "varied": {}, "control_run": None, "memory": {}, "cpus": [], "tasks_peak": None}


def _repeats(subject_dir: Path, tool: str) -> list[Path]:
    """The reports of a campaign's repetitions (``<tool>.r2.json`` on), in order."""
    found = [p for p in subject_dir.glob(f"{tool}.r*.json") if re.fullmatch(rf"{re.escape(tool)}\.r(\d+)\.json", p.name)
             and p.name != f"{tool}.r1.json"]
    return sorted(found, key=lambda p: int(p.name.removeprefix(f"{tool}.r").removesuffix(".json")))


def _campaign(subject_dir: Path, tool: str, problems: list[str], where: str) -> dict[str, Any] | None:
    """The first-run result of a planned campaign, or None after recording why there is none."""
    path, failed = subject_dir / f"{tool}.r1.json", subject_dir / f"{tool}.r1.failed.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not failed.exists():
        problems.append(f"{where}: no {tool} campaign")
    return None


def _failure(subject_dir: Path, tool: str) -> dict[str, Any] | None:
    """A failed campaign's record with its reason: the tool's command running past native.py's per-command limit
    when the campaign log shows it, otherwise the container's exit status."""
    path = subject_dir / f"{tool}.r1.failed.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    log = subject_dir / "native.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    executable = rf"(?:/native/{re.escape(VENV.get(tool, tool))}/bin/[^']*|{re.escape(tool)})"
    timeout = re.search(rf"TimeoutExpired: Command '\('{executable}'.*? timed out after (\d+) seconds", text)
    code = record.get("returncode")
    record["reason"] = (f"the tool's command ran past the {timeout.group(1)} s limit" if timeout else
                        code if isinstance(code, str) else f"exit {code}")
    return record


_STOP = re.compile(r"\b(errors?|fail(ed)?|stopping|could not|cannot)\b|no module named", re.IGNORECASE)
_NOISE = re.compile(r"(\x1b|\\u001b)\[[0-9;]*m|_{3,}")  # colors (raw, or escaped inside a JSON report); pytest rules


def _last_command(record: dict[str, Any]) -> str:
    """The tool's last command as the campaign log keeps it (``$ command``, ``rc=N``, the output's tail): its exit
    status and the error the tool's report gives (``stated_error``), else the last output line that states an
    error or a stop, else the last output line."""
    entry = (record.get("log") or [""])[-1]
    status = re.search(r"^rc=(-?\d+)$", entry, re.MULTILINE)
    lines = [_NOISE.sub("", line).strip() for line in entry.splitlines()[2:]]
    lines = [line for line in lines if line]
    stated = [" ".join(_NOISE.sub("", record["stated_error"]).split())] if record.get("stated_error") else []
    stated = stated or [line for line in lines if _STOP.search(line)]
    return f"exit {status.group(1) if status else 'unknown'}: {(stated or lines or [''])[-1][:160]}"


def _oracle_fingerprint(meta: dict[str, Any], where: str, problems: list[str], seen: set[str]) -> None:
    """Every reference run of a track must have one verdict semantics (``fingerprint.oracle_semantics``): a run
    without a code fingerprint predates the record format and is stale."""
    if not meta:
        return
    if not meta.get("fingerprint"):
        problems.append(f"{where}: reference oracle run without a code fingerprint (stale: judge it again)")
    else:
        seen.add(oracle_semantics(meta))


def engine() -> tuple[dict[str, Any], list[str]]:
    report: dict[str, Any] = {}
    problems: list[str] = []
    fingerprints: set[str] = set()
    for name in ENGINE_SUBJECTS:
        subject_dir = OUT / "engine" / name
        rows = _jsonl(subject_dir / "oracle.r1.jsonl")
        oracle = {r["aid"]: r for r in rows if "aid" in r}
        meta = next((r for r in rows if r.get("kind") == "meta"), {})
        unjudged = {r["aid"] for r in _jsonl(subject_dir / "union.r1.jsonl")} - oracle.keys()
        if not meta:
            problems.append(f"{name}: no reference oracle run")
        elif unjudged:
            problems.append(f"{name}: {len(unjudged)} artifacts without a reference verdict")
        _oracle_fingerprint(meta, name, problems, fingerprints)
        subject: dict[str, Any] = {"oracle_meta": {k: meta.get(k) for k in ("tests", "flaky", "suite_seconds",
                                                                             "fingerprint", "test_roots")}}
        for tool in NATIVE_TOOLS:
            first = _campaign(subject_dir, tool, problems, name)
            if first is None:
                failure = _failure(subject_dir, tool)
                subject[tool] = _no_result("missing" if failure is None else "failed", failure)
                continue
            repeats = [json.loads(path.read_text(encoding="utf-8")) for path in _repeats(subject_dir, tool)]
            native = [r for r in first.get("mutants", []) if r.get("native_id") != "<counts>"]
            verdicts = native_verdicts(first)
            agreement = compare(first, oracle)
            judged = any(verdict in VERDICTS for verdict in verdicts)
            subject[tool] = {
                "result": "ran" if judged else "no verdicts",
                "reason": None if judged else _last_command(first),
                "native_mutants": agreement["total"],
                "mapped": sum(1 for r in native if r.get("aid")),
                "wall_seconds": [first.get("wall_seconds"), *(r.get("wall_seconds") for r in repeats)],
                "cpu_seconds": [first.get("cpu_seconds"), *(r.get("cpu_seconds") for r in repeats)],
                "memory": {k: first.get(k) for k in ("largest_process_rss_mib", "cgroup_peak_mib", "cgroup_peak_scope",
                                                     "cgroup_peak_before_campaign_mib")},
                # (usable, visible) of every run: a tool sizes its default parallelism from the visible CPUs.
                "cpus": sorted({(r.get("cpus_usable"), r.get("cpus_visible")) for r in (first, *repeats)
                                if r.get("cpus_usable") is not None}),
                "tasks_peak": max((r.get("tasks_peak") or 0 for r in (first, *repeats)), default=0) or None,
                "memory_limit_reached": any((r.get("cgroup_peak_mib") or 0) >= ENGINE_MEMORY_MIB - 1
                                            for r in (first, *repeats)),
                "native_verdicts": verdicts,
                "agreement": agreement,
                "stability": [stability(first, r) for r in repeats],
                "varied": varied([first, *repeats]) if repeats else {},
                "control_run": first.get("control_run"),
            }
        report[name] = subject
    if len(fingerprints) > 1:
        problems.append(f"mixed oracle semantics {sorted(fingerprints)}")
    return report, problems


def conformance() -> tuple[dict[str, Any], list[str]]:
    """Per fixture and tool, the native verdicts of the mapped mutants that hit the trap against the hand-checked
    expectation: every native mutant counts (two native mutants of one mutated file keep both verdicts), a
    timeout passes for an expected kill only where the expectation says ``timeout_ok`` (a hang is the trap), and a
    mutant whose mutated file the oracle finds invalid (it does not compile) is an ``invalid mutant``, whatever the
    tool's verdict."""
    expectations = json.loads((BENCH / "conformance" / "expectations.json").read_text(encoding="utf-8"))
    report: dict[str, Any] = {}
    problems: list[str] = []
    fingerprints: set[str] = set()
    for fixture, rules in sorted(expectations.items()):
        out_dir = OUT / "conformance" / fixture
        rows = _jsonl(out_dir / "oracle.r1.jsonl")
        oracle_rows = {r["aid"]: r for r in rows if "aid" in r}
        _oracle_fingerprint(next((r for r in rows if r.get("kind") == "meta"), {}), fixture, problems, fingerprints)
        union = {r["aid"]: r for r in _jsonl(out_dir / "union.r1.jsonl")}
        module = trap_module(fixture)
        original = module.read_bytes()
        trap: dict[str, dict[str, Any]] = {}  # aid -> the rule it hits
        for aid, art in union.items():
            e = art["edit"]
            mutated = (original[: e["start"]] + e["replacement"].encode("utf-8") + original[e["end"] :]).decode("utf-8")
            line_no = original.count(b"\n", 0, e["start"]) + 1
            text = mutated.splitlines()[line_no - 1] if line_no <= len(mutated.splitlines()) else ""
            for rule in rules:
                if rule["line"] == line_no and re.search(rule["mutated_regex"], text):
                    trap[aid] = rule
        fixture_report: dict[str, Any] = {
            "trap_mutants": len(trap),
            "oracle": dict(Counter("ok" if oracle_rows.get(a, {}).get("status") == rule["expected"] else
                                   f"oracle {oracle_rows.get(a, {}).get('status')} expected {rule['expected']}"
                                   for a, rule in trap.items())),
        }
        for tool in NATIVE_TOOLS:
            data = _campaign(out_dir, tool, problems, fixture)
            if data is None:
                failure = _failure(out_dir, tool)
                fixture_report[tool] = {"result": "missing" if failure is None else "failed",
                                        "reason": (failure or {}).get("reason"), "trap_mutants_generated": 0,
                                        "outcome": {}}
                continue
            if not any(verdict in VERDICTS for verdict in native_verdicts(data)):
                fixture_report[tool] = {"result": "no verdicts", "reason": _last_command(data),
                                        "trap_mutants_generated": 0, "outcome": {}}
                continue
            outcome: Counter[str] = Counter()
            for row in data.get("mutants", []):
                rule = trap.get(row.get("aid", ""))
                if rule is None:
                    continue
                verdict, expected = row["verdict"], rule["expected"]
                if oracle_rows.get(row["aid"], {}).get("status") == "invalid":
                    # The tool's edit leaves a file that does not compile: its verdict judges a broken file.
                    outcome[f"invalid mutant (expected {expected})"] += 1
                elif verdict == expected or (verdict == "timeout" and expected == "killed" and rule.get("timeout_ok")):
                    outcome["correct"] += 1
                elif verdict == "timeout" and expected == "killed":
                    outcome["timeout (expected killed)"] += 1
                else:
                    outcome[f"{verdict} (expected {expected})"] += 1
            fixture_report[tool] = {"result": "ran", "trap_mutants_generated": sum(outcome.values()),
                                    "outcome": dict(outcome), "wall_seconds": data.get("wall_seconds"),
                                    "agreement": compare(data, oracle_rows)["counts"]}
        report[fixture] = fixture_report
    if len(fingerprints) > 1:
        problems.append(f"mixed oracle semantics {sorted(fingerprints)}")
    return report, problems


def _ratio(counts: Counter[str], part: str, *others: str) -> float:
    whole = counts.get(part, 0) + sum(counts.get(o, 0) for o in others)
    return counts.get(part, 0) / whole if whole else math.nan


def _ran(entry: dict[str, Any]) -> bool:
    """A campaign ran when the tool gave at least one of its mutants a verdict."""
    return entry["result"] == "ran"


COMPARABLE = ("agree: killed", "unconfirmed kill", "agree: survived", "missed kill")


def engine_totals(report: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Per tool, over the engine subjects: verdict comparison counts, native mutants, the subjects its campaign
    ran on, the largest memory peak, and wall time summed over the common subjects (those on which every planned
    tool's campaign ran), which are returned too: a campaign that judged nothing, or left no result, must not make
    its tool look fast. The wall time of a campaign is the median over its runs, with the smallest and the largest
    run beside it. CPU time stays out of the comparison: it is the resource usage of the reaped descendants of the
    campaign's commands, which misses every worker no waited-for process reaps."""
    common = [subject for subject, data in report.items() if all(_ran(data[t]) for t in NATIVE_TOOLS)]
    totals: dict[str, dict[str, Any]] = {}
    for subject, data in report.items():
        for tool in NATIVE_TOOLS:
            entry = data[tool]
            zero = math.nan if not common else 0.0
            total = totals.setdefault(tool, {"counts": Counter(), "wall": zero, "wall_low": zero, "wall_high": zero,
                                             "runs": 0, "mutants": 0, "ran": [], "peak_mib": 0.0,
                                             "peak_scopes": set(), "memory_limit": [], "cpus": set(), "tasks": 0})
            total["counts"].update(entry["agreement"]["counts"])
            total["mutants"] += entry["native_mutants"]
            memory = entry.get("memory") or {}
            if memory.get("cgroup_peak_mib") is not None:
                total["peak_mib"] = max(total["peak_mib"], memory["cgroup_peak_mib"])
                total["peak_scopes"].add(memory.get("cgroup_peak_scope"))
            if _ran(entry):
                total["ran"].append(subject)
            if entry.get("memory_limit_reached"):
                total["memory_limit"].append(subject)
            total["cpus"].update(tuple(pair) for pair in entry.get("cpus") or [])
            total["tasks"] = max(total["tasks"], entry.get("tasks_peak") or 0)
            if subject in common:
                walls = sorted(w for w in entry["wall_seconds"] if w is not None)
                total["wall"] += statistics.median(walls)
                total["wall_low"] += walls[0]
                total["wall_high"] += walls[-1]
                total["runs"] = max(total["runs"], len(walls))
    for total in totals.values():
        counts = total["counts"]
        total["false_kill"] = _ratio(counts, "unconfirmed kill", "agree: killed")
        total["false_survivor"] = _ratio(counts, "missed kill", "agree: survived")
        total["coverage_miss"] = _ratio(counts, "no coverage, killable", "agree (no coverage)")
        total["not_comparable"] = sum(v for k, v in counts.items() if k not in COMPARABLE)
    return totals, common


def engine_table(report: dict[str, Any]) -> str:
    """The table twin of the engine figures, with every planned campaign's outcome per subject."""
    totals, common = engine_totals(report)
    scopes = sorted({s for v in totals.values() for s in v["peak_scopes"] if s}) or ["n/a"]
    lines = [(f"Wall time: the median over a campaign's runs (smallest and largest in brackets), summed over the "
              f"projects on which every campaign ran ({', '.join(common) or 'none'}); per campaign and first run in "
              "the second table. CPUs: those the container may use against those the tool's process sees "
              "(`os.cpu_count()`), from which tools size their parallelism. Peak tasks: the most processes and threads "
              "the container held (cgroup pids.peak, recorded from the repeated runs on). A peak memory at the "
              "container's 3 GiB limit measures a test that allocates without bound, not the tool. "
              "False kills: unconfirmed kills / "
              "(agree: killed + unconfirmed kills); false survivors: missed kills / (agree: survived + missed kills); "
              "not comparable: every other native mutant (unmapped, no reference verdict, the oracle's non-verdicts, "
              "the tool's timeouts, errors, skips and no-coverage verdicts, verdicts the tool reports only as a "
              f"total). Peak memory: the container's memory peak in MiB (cgroup memory.peak, {'; '.join(scopes)}), "
              "the largest over the projects in the first table; it counts every process the campaign starts and "
              "the page cache of the files they read and write, so a mutant whose tests allocate without bound "
              "raises it to the container's memory limit. The CPU time in "
              "engine_track.json counts only the processes a campaign waited for."), "",
             ("| tool | projects run | native mutants | agree: killed | unconfirmed kills | agree: survived | "
              "missed kills | not comparable | false kills | false survivors | wall time | CPUs usable of visible "
              "| peak tasks | peak memory |"),
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def share(value: float) -> str:
        return "n/a" if math.isnan(value) else f"{100 * value:.1f}%"

    def seconds(value: float) -> str:
        return "n/a" if math.isnan(value) else f"{value:.0f} s"

    for tool in sorted(totals):
        v = totals[tool]
        c = v["counts"]
        wall = seconds(v["wall"]) + (f" ({v['wall_low']:.0f}–{v['wall_high']:.0f}, {v['runs']} runs)"
                                     if v["runs"] > 1 else "")
        cpus = ", ".join(f"{usable} of {visible}" for usable, visible in sorted(v["cpus"])) or "n/a"
        limit = f" (limit reached: {', '.join(v['memory_limit'])})" if v["memory_limit"] else ""
        lines.append(f"| {tool} | {len(v['ran'])}/{len(report)} | {v['mutants']} | {c.get('agree: killed', 0)} | "
                     f"{c.get('unconfirmed kill', 0)} | {c.get('agree: survived', 0)} | {c.get('missed kill', 0)} | "
                     f"{v['not_comparable']} | {share(v['false_kill'])} | {share(v['false_survivor'])} | "
                     f"{wall} | {cpus} | {v['tasks'] or 'n/a'} | {v['peak_mib']:.0f} MiB{limit} |")
    lines += ["", "Outcomes: a campaign that ran and gave verdicts (ran), one that gave none or left no report "
              "(did not run out of the box), and among those that ran, the ones whose comparable verdicts all agree "
              "with the reference oracle.", "",
              "| tool | ran, all comparable verdicts agree | ran, some disagree | did not run out of the box |",
              "|---|---|---|---|"]
    for tool in sorted(totals):
        agree = [s for s, d in report.items() if _ran(d[tool]) and not _disagrees(d[tool])]
        disagree = [s for s, d in report.items() if _ran(d[tool]) and _disagrees(d[tool])]
        failed = [s for s, d in report.items() if not _ran(d[tool])]
        cells = [", ".join(group) or "–" for group in (agree, disagree, failed)]
        lines.append(f"| {tool} | {' | '.join(cells)} |")
    stable = [(s, t, e) for s, d in report.items() for t in NATIVE_TOOLS if (e := d[t]).get("stability")]
    if stable:
        lines += ["", "Repeated campaigns: per repetition, the mutants it shares with the first run (by mutated file, "
                  "else by file and native id) and how many of them changed verdict; over all runs, the mutants every "
                  "run has whose verdicts differ, by the verdicts they got.", "",
                  "| project | tool | runs | common mutants | changed verdict | varied over all runs |",
                  "|---|---|---|---|---|---|"]
        for subject, tool, entry in stable:
            runs = entry["stability"]
            variation = ", ".join(f"{verdicts} {n}" for verdicts, n in entry.get("varied", {}).items()) or "–"
            lines.append(f"| {subject} | {tool} | {len(runs) + 1} | {', '.join(str(r['common']) for r in runs)} "
                         f"| {', '.join(str(r['changed']) for r in runs)} | {variation} |")
    lines += ["",
              "| project | tool | campaign | wall time | peak memory | native verdicts | comparison | control run |",
              "|---|---|---|---|---|---|---|---|"]
    for subject, data in report.items():
        for tool in NATIVE_TOOLS:
            entry = data[tool]
            verdicts = ", ".join(f"{k} {n}" for k, n in sorted(entry["native_verdicts"].items()))
            comparison = ", ".join(f"{k} {n}" for k, n in sorted(entry["agreement"]["counts"].items()))
            # A project's own pytest options may colour the summary line (humanize: --color=yes).
            control = re.sub(r"\x1b\[[0-9;]*m", "", (entry.get("control_run") or {}).get("summary", ""))
            outcome = entry["result"] + (f" ({entry['reason']})".replace("|", "\\|") if entry.get("reason") else "")
            wall = entry["wall_seconds"][0]
            peak = (entry.get("memory") or {}).get("cgroup_peak_mib")
            lines.append(f"| {subject} | {tool} | {outcome} | {seconds(math.nan if wall is None else wall)} | "
                         f"{'n/a' if peak is None else f'{peak:.0f} MiB'} | {verdicts} | {comparison} | {control} |")
    return "\n".join(lines) + "\n"


def _disagrees(entry: dict[str, Any]) -> bool:
    counts = entry["agreement"]["counts"]
    return bool(counts.get("unconfirmed kill") or counts.get("missed kill"))


def engine_figures(report: dict[str, Any], images: Path, focus: str = "") -> list[str]:
    totals, common = engine_totals(report)
    subjects = len(report)
    written = []
    specs = [
        ("bench-engine-false-kills", "Kills the reference oracle does not reproduce",
         f"share of the tool's 'killed' verdicts the reference oracle does not kill, among comparable verdicts · "
         f"{subjects} projects at HEAD", {t: v["false_kill"] for t, v in totals.items()}, True, ""),
        ("bench-engine-false-survivors", "Survivors the reference oracle kills",
         f"share of the tool's 'survived' verdicts the reference oracle kills, among comparable verdicts · "
         f"{subjects} projects at HEAD", {t: v["false_survivor"] for t, v in totals.items()}, True, ""),
        ("bench-engine-time", "Wall time of the tool's own campaign",
         (f"{', '.join(common) or 'no project every campaign ran on'} · median of "
          f"{max((v['runs'] for v in totals.values()), default=1)} runs, smallest to largest as whiskers, 4 vCPUs · "
          "lower is faster"),
         {t: v["wall"] for t, v in totals.items()}, False, "s"),
    ]
    ranges = {t: (v["wall_low"], v["wall_high"]) for t, v in totals.items() if v["runs"] > 1}
    for name, title, subtitle, values, percent, unit in specs:
        rows = [svg.Row(t, v, *(ranges.get(t, (None, None)) if name == "bench-engine-time" else (None, None)),
                        emphasis=t == focus) for t, v in values.items()]
        known = sorted((r for r in rows if not math.isnan(r.value)), key=lambda r: r.value, reverse=percent)
        rows = known + [svg.Row(r.label, math.nan, emphasis=r.emphasis, note="no comparable verdicts" if percent
                                else "not in every project") for r in rows if math.isnan(r.value)]
        for theme in svg.THEMES:
            path = images / f"{name}.{theme.name}.svg"
            path.write_text(svg.ranked_bars(title, subtitle, rows, theme, percent=percent, unit=unit), encoding="utf-8")
            written.append(str(path))
    return written


CONFORMANCE_STATUSES = [
    svg.Status("ok", "as expected", "✓", "gray_light"),
    svg.Status("timeout", "timeout for a kill", "T", "gray"),
    svg.Status("wrong", "wrong verdict", "✗", "second"),
    svg.Status("none", "no trap mutant", "–", "surface"),
    svg.Status("failed", "no result", "!", "surface"),
]


def conformance_figure(report: dict[str, Any], images: Path, focus: str = "") -> list[str]:
    fixtures = sorted(report)
    tools = sorted(NATIVE_TOOLS)
    cells, notes = [], {}
    for i, tool in enumerate(tools):
        row = []
        for j, fixture in enumerate(fixtures):
            entry = report[fixture][tool]
            if entry["result"] != "ran":
                row.append("failed")
                continue
            if not entry["trap_mutants_generated"]:
                row.append("none")
                continue
            outcome = entry["outcome"]
            wrong = [k for k in outcome if k not in {"correct", "timeout (expected killed)"}]
            row.append("wrong" if wrong else "timeout" if "timeout (expected killed)" in outcome else "ok")
            notes[(i, j)] = ", ".join(f"{k}: {v}" for k, v in sorted(outcome.items()))
        cells.append(row)
    written = []
    for theme in svg.THEMES:
        path = images / f"bench-conformance.{theme.name}.svg"
        path.write_text(svg.status_matrix("Execution traps", "each project hides one trap; hand-checked expected "
                                          "verdicts for the tool's mapped mutants that hit it", tools,
                                          [f.removeprefix("cf_") for f in fixtures], cells, CONFORMANCE_STATUSES,
                                          theme, emphasis=focus, notes=notes), encoding="utf-8")
        written.append(str(path))
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("track", choices=("engine", "conformance"))
    parser.add_argument("--results", required=True)
    parser.add_argument("--images", required=True)
    parser.add_argument("--preview", action="store_true", help="report the gaps instead of refusing")
    parser.add_argument("--focus", default="", choices=("", *NATIVE_TOOLS), help="highlight one tool in the figures")
    args = parser.parse_args()
    results, images = Path(args.results), Path(args.images)
    report, problems = engine() if args.track == "engine" else conformance()
    if problems and not args.preview:
        raise SystemExit("not publishable:\n  " + "\n  ".join(problems[:40]) + f"\n  ({len(problems)} problems)")
    results.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    name = "engine_track.json" if args.track == "engine" else "conformance.json"
    (results / name).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if args.track == "engine":
        (results / "engine.md").write_text(engine_table(report), encoding="utf-8")
        written = engine_figures(report, images, args.focus)
    else:
        written = conformance_figure(report, images, args.focus)
    for problem in problems:
        print(f"gap: {problem}")
    print(f"wrote {len(written)} figures")


if __name__ == "__main__":
    main()
