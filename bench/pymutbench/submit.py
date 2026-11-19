"""Evaluate a tool that is not in the harness from its own mutants (``bench/SUBMISSION.md``).

    python3 -m pymutbench.submit export-inputs --out DIR
    python3 -m pymutbench.submit evaluate --tool NAME --version V --mutants DIR [--buggy-mutants DIR] [--judge]

``export-inputs`` writes per bug ``<bug-id>/fixed/<path>`` (the files to mutate: the fixed program),
``<bug-id>/buggy/<path>`` (the same files before the fix), their scopes and the commits to rebuild the whole
tree. The tool's author runs the generator on them and writes, per bug, ``<bug-id>.jsonl`` in the mutant format
(``bench/mutant.schema.json``: one record per native mutant, the complete mutated text or the reason there is
none), for the fixed files and, optionally, for the buggy ones.

``evaluate`` normalizes the records exactly as the harness normalizes its own adapters' output (the same scope
filter and accounting), merges them into each bug's union, and with ``--judge`` runs the reference oracle on the
artifacts no tool had before, the witness re-judgement and the buggy-side stage (the subjects' images and
clones are needed; artifacts already judged cost nothing). It then writes the tool's card: its rank interval
among the published tools, its measures with intervals and its paired difference from every published tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import CORPUS, RESULTS, __version__, fixes, fixlines
from pymutbench.artifacts import Artifact
from pymutbench.generate import normalize
from pymutbench.merge import merge_bug
from pymutbench.metrics import TOOLS
from pymutbench.orchestrate import OUT
from pymutbench.scope import Region, regions
from pymutbench.subjects import SUBJECTS


def _bugs() -> list[tuple[str, dict[str, Any], Path]]:
    found = []
    for name in sorted(SUBJECTS):
        path = OUT / "bugs" / f"{name}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            bug = json.loads(line)
            found.append((f"{name}@{bug['commit'][:12]}", bug, OUT / "gen" / name / bug["commit"][:12]))
    return found


def _buggy_scope(fix: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """The definitions of the buggy files that the fix removed or rewrote lines of."""
    found = {}
    for path, record in fix["files"].items():
        if record["status"] == "M" and record.get("buggy") is not None:
            found[path] = [
                [r.start, r.end, r.kind, r.name]
                for r in regions(record["buggy"], fixlines.buggy_lines(record.get("diff_u0", "")))
            ]
    return found


def export_inputs(out: Path) -> int:
    count = 0
    for cid, bug, directory in _bugs():
        if not (directory / "fix.json").exists():
            continue
        fix = fixes.load(directory)
        target = out / cid
        for side in ("fixed", "buggy"):
            for path, record in fix["files"].items():
                if record.get(side) is not None and record["status"] != "D":
                    file = target / side / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(record[side], encoding="utf-8")
        (target / "scope.json").write_text((directory / "scope.json").read_text(encoding="utf-8"), encoding="utf-8")
        (target / "buggy_scope.json").write_text(json.dumps(_buggy_scope(fix)), encoding="utf-8")
        subject = SUBJECTS[bug["subject"]]
        (target / "tree.json").write_text(
            json.dumps(
                {
                    "repository": f"https://github.com/{subject.repo}",
                    "commit": bug["commit"],
                    "parent": bug["parent"],
                    "fixed_tree": "the parent commit with these files taken from the commit",
                    "files": [f["path"] for f in bug["src_files"]],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        count += 1
    return count


def _import(
    tool: str, version: str, raw: Path, files: dict[str, str], scope: dict[str, list[list[Any]]], target: Path
) -> dict[str, Any]:
    """Normalize one bug's records against ``files`` (path -> text) and ``scope``: ``<tool>.jsonl`` and its ledger
    in ``target``."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(text.encode("utf-8"))
        bug_regions = {
            path: [Region(start, end, kind, name) for start, end, kind, name in found] for path, found in scope.items()
        }
        artifacts, ledger = normalize(tool, root, raw, bug_regions)
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()[:16]
    ledger.update(
        {
            "failed": False,
            "failure": None,
            "rc": 0,
            "fingerprint": f"external:{tool}=={version}:{digest}",
            "image": "submitted",
        }
    )
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{tool}.jsonl").write_text(
        "".join(json.dumps(Artifact.to_json(a)) + "\n" for a in artifacts), encoding="utf-8"
    )
    (target / f"{tool}.ledger.json").write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    return ledger


def import_mutants(tool: str, version: str, mutants: Path, buggy_mutants: Path | None) -> dict[str, int]:
    """Every bug with a record file becomes evaluated: its fixed-side mutants merge into the union; a bug without
    one counts as a generation failure of the tool (unavailable)."""
    if tool in TOOLS:
        raise SystemExit(f"{tool} is a registered tool: name a new one")
    counts = {"bugs": 0, "missing": 0, "emitted": 0}
    for cid, _bug, directory in _bugs():
        fix = fixes.load(directory)
        raw = mutants / f"{cid}.jsonl"
        if not raw.exists():
            counts["missing"] += 1
            (directory / f"{tool}.jsonl").write_text("", encoding="utf-8")
            (directory / f"{tool}.ledger.json").write_text(
                json.dumps(
                    {
                        "failed": True,
                        "failure": "no records submitted",
                        "emitted": 0,
                        "materialized": 0,
                        "unmaterializable": {},
                        "in_scope": 0,
                        "out_of_scope": 0,
                        "identical": 0,
                        "unmaterializable_in_scope": 0,
                        "fingerprint": f"external:{tool}=={version}",
                    }
                ),
                encoding="utf-8",
            )
            continue
        fixed = {p: r["fixed"] for p, r in fix["files"].items() if r.get("fixed") is not None}
        scope = json.loads((directory / "scope.json").read_text(encoding="utf-8"))
        ledger = _import(tool, version, raw, fixed, scope, directory)
        counts["bugs"] += 1
        counts["emitted"] += ledger["emitted"]
        merge_bug(directory, (*TOOLS, tool))
        if buggy_mutants is not None and (buggy_mutants / f"{cid}.jsonl").exists():
            buggy = {
                p: r["buggy"] for p, r in fix["files"].items() if r["status"] == "M" and r.get("buggy") is not None
            }
            _import(tool, version, buggy_mutants / f"{cid}.jsonl", buggy, _buggy_scope(fix), directory / "buggy")
    return counts


def card(tool: str, version: str, results_dir: Path) -> dict[str, Any]:
    """The tool's row among the published tools on the corpus: rank intervals, measures with intervals and the
    paired difference from every published tool (pre-fix detection)."""
    from pymutbench.report import PRIMARY, aggregate, load

    measured = (*TOOLS, tool)
    agg = aggregate(load(preview=True, tools=measured), measured)
    row = agg["tools"][tool]
    pairs = [p for p in agg["pairs"] if tool in (p["first"], p["second"])]
    result = {
        "tool": tool,
        "version": version,
        "corpus": CORPUS,
        "harness": __version__,
        "results_release": RESULTS,
        "oracle": agg["oracle"],
        "configuration": "as submitted",
        "bugs": agg["bugs"],
        "available_bugs": row["available_bugs"],
        "detection": row["detection"],
        "r10": row["r"][10],
        "r10_ci": row["r10_ci"],
        "s3": row["s"][3],
        "s3_ci": row["s3_ci"],
        "emitted_per_bug_median": row["emitted_per_bug_median"],
        "pairs": [
            {
                "other": p["second"] if p["first"] == tool else p["first"],
                "difference": p["difference"] if p["first"] == tool else -p["difference"],
                "ci": p["ci"] if p["first"] == tool else [-p["ci"][1], -p["ci"][0]],
                "p": p["p"],
                "p_holm": p["p_holm"],
            }
            for p in pairs
        ],
        "note": f"{PRIMARY} detection is the primary measure; see docs/benchmark.md",
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{tool}.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export-inputs")
    p.add_argument("--out", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--tool", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--mutants", required=True, help="a directory of <bug-id>.jsonl files for the fixed files")
    p.add_argument("--buggy-mutants", default="", help="the same for the buggy files (the third condition)")
    p.add_argument("--judge", action="store_true", help="judge the new artifacts (needs the images and clones)")
    p.add_argument("--jobs", type=int, default=2)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--cards", default="cards", help="where the tool's card is written")
    args = parser.parse_args()
    if args.cmd == "export-inputs":
        print(f"wrote the inputs of {export_inputs(Path(args.out))} bugs")
        return
    counts = import_mutants(
        args.tool, args.version, Path(args.mutants), Path(args.buggy_mutants) if args.buggy_mutants else None
    )
    print(f"imported {counts}", flush=True)
    if args.judge:
        from pymutbench import pipeline

        for name in sorted(SUBJECTS):
            pipeline.judge(name, args.jobs)
            pipeline.confirm(name, args.jobs)
        pipeline.prefix([], args.jobs, args.parallel, tools=(*TOOLS, args.tool))
    result = card(args.tool, args.version, Path(args.cards))
    d = result["detection"]
    print(
        f"{args.tool} {args.version}: pre-fix {d['prefix']['rate']:.1%} {d['prefix']['ci']} "
        f"rank {d['prefix']['rank']}, "
        f"fixed program {d['fixed']['rate']:.1%}"
    )


if __name__ == "__main__":
    main()
