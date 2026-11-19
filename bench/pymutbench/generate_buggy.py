"""Generate a tool's mutants of the buggy files of a bug (runs inside the tools image): the third condition.

A witness on lines the fix rewrote exists before the fix when the tool's own generator, run on the buggy parent,
emits the same change at the corresponding place (``prefix``). Per bug the parent tree is checked out, each listed
tool's adapter runs on the source files the fix modified, and its mutants are normalized exactly as on the fixed
side; the scope is the definitions of the buggy file that the fix touched. Outputs go to ``<bug>/buggy/``: one
artifact file and one ledger per tool.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.artifacts import Artifact
from pymutbench.fingerprint import buggy_generation_fingerprint
from pymutbench.generate import TOOLS, normalize, run_adapter
from pymutbench.gitops import add_worktree, git, remove_worktree
from pymutbench.scope import changed_lines, regions

FINGERPRINTS = {tool: buggy_generation_fingerprint(script) for tool, (_python, script) in TOOLS.items()}


def generate_bug(repo: Path, work: Path, out_dir: Path, bug: dict[str, Any], tools: list[str]) -> None:
    commit, parent = bug["commit"], bug["parent"]
    files = [f["path"] for f in bug["src_files"] if f["status"] == "M"]
    tree = work / f"{commit[:12]}-parent"
    add_worktree(repo, tree, parent)
    try:
        bug_regions = {}
        for path in files:
            # The buggy file's lines the fix removed or rewrote: the new side of the reversed diff.
            diff = git(repo, "diff", "--no-renames", "-U0", commit, parent, "--", path)
            bug_regions[path] = regions((tree / path).read_text(encoding="utf-8"), changed_lines(diff))
        target = out_dir / commit[:12] / "buggy"
        target.mkdir(parents=True, exist_ok=True)
        ranges = {path: [[r.start, r.end] for r in rs] for path, rs in bug_regions.items()}
        (target / "scope.json").write_text(
            json.dumps({p: [[r.start, r.end, r.kind, r.name] for r in rs] for p, rs in bug_regions.items()}),
            encoding="utf-8",
        )

        def one(tool: str) -> str:
            raw = target / f"{tool}.raw.jsonl"
            artifacts_path, ledger_path = target / f"{tool}.jsonl", target / f"{tool}.ledger.json"
            ledger_path.unlink(missing_ok=True)  # the ledger, written last, marks a complete result
            run = run_adapter(tool, tree, files, raw, ranges)
            failure = None if run["rc"] == 0 and raw.exists() else f"rc={run['rc']}"
            artifacts: list[Artifact] = []
            ledger: dict[str, Any] = {}
            if failure is None:
                try:
                    artifacts, ledger = normalize(tool, tree, raw, bug_regions)
                except json.JSONDecodeError as exc:
                    failure = f"malformed output: {exc}"
            if failure is not None:
                artifacts, ledger = (
                    [],
                    {
                        "emitted": 0,
                        "materialized": 0,
                        "unmaterializable": {},
                        "in_scope": 0,
                        "out_of_scope": 0,
                        "identical": 0,
                        "unmaterializable_in_scope": 0,
                    },
                )
            ledger.update(run)
            ledger.update(
                {
                    "failed": failure is not None,
                    "failure": failure,
                    "fingerprint": FINGERPRINTS[tool],
                    "image": os.environ.get("PMB_IMAGE", ""),
                    "side": "parent",
                }
            )
            with artifacts_path.open("w", encoding="utf-8") as fh:
                for art in artifacts:
                    fh.write(json.dumps(art.to_json()) + "\n")
            ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
            raw.unlink(missing_ok=True)
            return (
                f"{commit[:10]} {tool:<16} buggy side emitted={ledger['emitted']:>5} in_scope={ledger['in_scope']:>4}"
            )

        with ThreadPoolExecutor(4) as pool:
            for line in pool.map(one, tools):
                print(line, flush=True)
    finally:
        remove_worktree(repo, tree)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bugs", required=True, help="JSONL of bug records, each with the tools to run (``tools``)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", default="/work/repo")
    parser.add_argument("--work", default="/work/gen")
    args = parser.parse_args()
    repo, work, out = Path(args.repo), Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    for line in Path(args.bugs).read_text(encoding="utf-8").splitlines():
        if line.strip():
            bug = json.loads(line)
            generate_bug(repo, work, out, bug, bug["tools"])


if __name__ == "__main__":
    main()
