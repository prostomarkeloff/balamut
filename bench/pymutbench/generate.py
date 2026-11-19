"""Generate every tool's mutants for the admitted bugs of one subject (runs inside the tools image).

Per bug the historical tree is built (the parent commit with the fix's source files), each tool's adapter runs
with that tool's own interpreter over the fixed files the fix touched, and its native mutants are normalized
into artifacts. The scope filter (``pymutbench.scope``) keeps the artifacts whose edit lies inside a definition
the fix touched; the accounting ledger keeps every native mutant's fate: emitted, materialized, unmaterializable
(with the reason), in scope or not.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.artifacts import Artifact, Edit, artifact_id, edit_lines, from_mutated
from pymutbench.fingerprint import generation_fingerprint
from pymutbench.gitops import FileChange, add_worktree, git, remove_worktree, take_files
from pymutbench.registry import REGISTRY
from pymutbench.scope import changed_lines, in_scope, regions

ADAPTERS = Path(__file__).resolve().parent / "adapters"
TOOLS: dict[str, tuple[str, str]] = {name: (tool.python, tool.adapter) for name, tool in REGISTRY.items()}
# The code this process loaded, fixed at start: one fingerprint per tool (shared files and its adapter).
FINGERPRINTS = {tool: generation_fingerprint(script) for tool, (_python, script) in TOOLS.items()}


OOM_KILLED = -9  # SIGKILL: the container VM's out-of-memory killer under the load of concurrent adapters
ATTEMPTS = 2
ADAPTER_TIMEOUT = 3600


def run_adapter(tool: str, root: Path, files: list[str], out: Path,
                scope: dict[str, list[list[int]]]) -> dict[str, Any]:
    python, script = TOOLS[tool]
    started = time.monotonic()
    argv = [python, str(ADAPTERS / script), "--root", str(root), "--files", ",".join(files), "--out", str(out),
            "--scope", json.dumps(scope)]
    for attempt in range(1, ATTEMPTS + 1):
        # The adapter is deterministic and rewrites its output file, so a killed run is repeated as a whole.
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=ADAPTER_TIMEOUT,
                                      check=False)
            except subprocess.TimeoutExpired as exc:
                stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
                return {"rc": "timeout", "seconds": round(time.monotonic() - started, 2), "stderr": stderr[-2000:],
                        "attempts": attempt}
        if proc.returncode != OOM_KILLED:
            break
    return {"rc": proc.returncode, "seconds": round(time.monotonic() - started, 2), "stderr": proc.stderr[-2000:],
            "attempts": attempt}


def _records(raw: Path) -> Iterator[dict[str, Any]]:
    """The adapter's records one at a time: every record holds a complete mutated file."""
    with raw.open(encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                yield json.loads(line)


def normalize(tool: str, root: Path, raw: Path,
              bug_regions: dict[str, list[Any]]) -> tuple[list[Artifact], dict[str, Any]]:
    ledger: dict[str, Any] = {"emitted": 0, "materialized": 0, "unmaterializable": {}, "in_scope": 0,
                              "out_of_scope": 0, "identical": 0, "unmaterializable_in_scope": 0}
    artifacts: list[Artifact] = []
    originals: dict[str, bytes] = {}
    if not raw.exists():
        return artifacts, ledger
    for record in _records(raw):
        ledger["emitted"] += 1
        if record.get("status") != "ok" or "mutated" not in record:
            reason = record.get("status", "missing_text")
            ledger["unmaterializable"][reason] = ledger["unmaterializable"].get(reason, 0) + 1
            # It still cost the tool an emission: counted in the in-scope budget when its line is known.
            line = record.get("line", record.get("gate_line"))
            if isinstance(line, int) and in_scope(bug_regions.get(record["path"], []), line, line):
                ledger["unmaterializable_in_scope"] += 1
            continue
        path = record["path"]
        original = originals.setdefault(path, (root / path).read_bytes())
        if b"\r" in original:
            # Adapters read text with universal newlines: a CR in the file would turn every local edit into a
            # whole-file span. Recorded, so a subject with CR line endings is visible in the ledger.
            ledger["carriage_return_in_source"] = True
        mutated = record["mutated"].encode("utf-8")
        origin = {"tool": tool, "native_id": record["native_id"], "operator": record.get("operator", "")}
        if mutated == original:
            # A no-op emission still costs the tool a run: kept (the oracle marks it noop) when the line the tool
            # reports lies in scope.
            ledger["identical"] += 1
            line = record.get("line")
            if isinstance(line, int) and in_scope(bug_regions.get(path, []), line, line):
                ledger["in_scope"] += 1
                artifacts.append(Artifact(path, Edit(0, 0, ""), artifact_id(path, original), [origin]))
            continue
        ledger["materialized"] += 1
        if record.get("gate_line") is not None:
            origin["gate_line"] = record["gate_line"]
        art = from_mutated(path, original, mutated, origin)
        first, last = edit_lines(original, art.edit)
        if in_scope(bug_regions.get(path, []), first, last):
            ledger["in_scope"] += 1
            artifacts.append(art)
        else:
            ledger["out_of_scope"] += 1
    return artifacts, ledger


def generate_bug(repo: Path, work: Path, out_dir: Path, bug: dict[str, Any], tools: list[str]) -> None:
    commit, parent = bug["commit"], bug["parent"]
    changes = [FileChange(f["status"], f["path"], f["added"], f["removed"]) for f in bug["src_files"]]
    files = [c.path for c in changes if c.status != "D"]
    tree = work / commit[:12]
    add_worktree(repo, tree, parent)
    try:
        take_files(tree, commit, changes, from_side="fix")
        bug_regions = {}
        for path in files:
            diff = git(repo, "diff", "--no-renames", "-U0", parent, commit, "--", path)
            found = regions((tree / path).read_text(encoding="utf-8"), changed_lines(diff))
            bug_regions[path] = found
        target = out_dir / commit[:12]
        target.mkdir(parents=True, exist_ok=True)
        scope = {path: [[r.start, r.end, r.kind, r.name] for r in rs] for path, rs in bug_regions.items()}
        (target / "scope.json").write_text(json.dumps(scope), encoding="utf-8")
        ranges = {path: [[r.start, r.end] for r in rs] for path, rs in bug_regions.items()}

        def one(tool: str) -> str:
            raw = target / f"{tool}.raw.jsonl"
            artifacts_path, ledger_path = target / f"{tool}.jsonl", target / f"{tool}.ledger.json"
            ledger_path.unlink(missing_ok=True)  # the ledger, written last, marks a complete result
            artifacts_path.unlink(missing_ok=True)
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
                # The tool is unavailable for this bug: no partial artifacts, the failure and its evidence recorded.
                artifacts, ledger = [], {"emitted": 0, "materialized": 0, "unmaterializable": {}, "in_scope": 0,
                                         "out_of_scope": 0, "identical": 0, "unmaterializable_in_scope": 0}
            ledger.update(run)
            ledger["failed"] = failure is not None
            ledger["failure"] = failure
            ledger["fingerprint"] = FINGERPRINTS[tool]
            ledger["image"] = os.environ.get("PMB_IMAGE", "")
            with artifacts_path.open("w", encoding="utf-8") as fh:
                for art in artifacts:
                    fh.write(json.dumps(art.to_json()) + "\n")
            ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
            raw.unlink(missing_ok=True)  # the normalized artifacts and the ledger are the record
            return (f"{commit[:10]} {tool:<16} emitted={ledger['emitted']:>5} in_scope={ledger['in_scope']:>4} "
                    f"unmat={sum(ledger['unmaterializable'].values()):>3} rc={run['rc']} {run['seconds']}s")

        # The adapters are independent processes reading the same tree: they run side by side.
        with ThreadPoolExecutor(4) as pool:
            for line in pool.map(one, tools):
                print(line, flush=True)
    finally:
        remove_worktree(repo, tree)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bugs", required=True, help="JSONL of admitted bug records")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", default="/work/repo")
    parser.add_argument("--work", default="/work/gen")
    parser.add_argument("--tools", default=",".join(TOOLS))
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    repo, work, out = Path(args.repo), Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    tools = [t for t in args.tools.split(",") if t]
    bugs = [json.loads(line) for line in Path(args.bugs).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        bugs = [b for b in bugs if b["commit"].startswith(tuple(args.only.split(",")))]
    for bug in bugs:
        generate_bug(repo, work, out, bug, tools)


if __name__ == "__main__":
    main()
