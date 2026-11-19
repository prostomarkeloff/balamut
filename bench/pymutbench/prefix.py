"""The buggy-side counterparts of a bug's witnesses: the third condition of a pre-fix witness.

For every witness of the fixed program (``metrics``), per tool that emitted it:

* on lines the fix inserted it has no counterpart: that code did not exist before the fix;
* in code the fix did not touch, its counterpart is the same change written into the buggy parent's file
  (``fixlines.transplant``; ``exists: transplant``);
* on lines the fix rewrote, its counterparts are the mutants the same tool's generator emits on the buggy file
  that make the same change at the corresponding place (``generate_buggy``; ``exists: generated``, or
  ``absent`` when the generator emits none).

The counterparts form the bug's parent union (``parent.union.jsonl``, mutants of the buggy files), which the
oracle judges on the parent tree (``oracle.py --side parent``); ``prefix.json`` maps every witness and tool to its
counterparts. A counterpart's origins keep the tool's own origin, with a coverage gate line moved to the buggy
file's numbering, so a coverage-gated tool is judged by what the historical suite executes on the buggy code.
"""

from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import fixlines
from pymutbench.artifacts import Artifact, from_mutated, merge, write_jsonl
from pymutbench.metrics import DEFAULT_DROP

_TOKEN = re.compile(r"\w+|\*\*=?|//=?|>>=?|<<=?|[<>=!]=|->|:=|[-+*/%&|^@]=|\S")


def build(directory: Path, fix: dict[str, Any], witnesses: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Write ``parent.union.jsonl`` and ``prefix.json`` for one bug; ``witnesses`` is the bug's witness detail
    (``BugResult.details["witnesses"]``). Returns the counts of the outcomes."""
    buggy_side = _buggy_side(directory)
    counts: dict[str, int] = {}
    artifacts: list[Artifact] = []
    record: dict[str, Any] = {"witnesses": {}}
    for aid, detail in sorted(witnesses.items()):
        cls, path = detail["class"], detail["path"]
        files = fix["files"].get(path, {})
        fixed, buggy = files.get("fixed"), files.get("buggy")
        file_hunks = fixlines.hunks(files.get("diff_u0", ""))
        entry: dict[str, Any] = {"class": cls, "tools": {}}
        for tool, info in sorted(detail["tools"].items()):
            outcome: dict[str, Any]
            if cls == "inserted" or buggy is None:
                outcome = {"exists": "absent"}
            elif cls == "unchanged":
                outcome = _transplanted(
                    path,
                    fixed.encode("utf-8"),
                    buggy.encode("utf-8"),
                    detail["edit"],
                    file_hunks,
                    aid,
                    info["origins"],
                    artifacts,
                )
            elif tool not in buggy_side:
                outcome = {"exists": "pending"}  # the tool's buggy-side generation has not run
            elif buggy_side[tool] is None:
                outcome = {"exists": "failed"}
            else:
                outcome = _generated(
                    path,
                    fixed.encode("utf-8"),
                    buggy.encode("utf-8"),
                    detail["edit"],
                    file_hunks,
                    buggy_side[tool] or [],
                    artifacts,
                    aid,
                )
            entry["tools"][tool] = outcome
            counts[f"{cls}: {outcome['exists']}"] = counts.get(f"{cls}: {outcome['exists']}", 0) + 1
        record["witnesses"][aid] = entry
    write_jsonl(directory / "parent.union.jsonl", merge(artifacts))
    (directory / "prefix.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return counts


def _transplanted(
    path: str,
    fixed: bytes,
    buggy: bytes,
    edit: dict[str, Any],
    file_hunks: list[fixlines.Hunk],
    witness: str,
    origins: list[dict[str, Any]],
    into: list[Artifact],
) -> dict[str, Any]:
    mutated = fixlines.transplant(fixed, edit["start"], edit["end"], edit["replacement"], buggy, file_hunks)
    if mutated is None:
        return {"exists": "failed"}
    moved = []
    for origin in origins:
        entry = {**origin, "witness": witness, "via": "transplant"}
        if "gate_line" in entry:
            entry["gate_line"] = fixlines.to_parent_line(int(entry["gate_line"]), file_hunks)
        moved.append(entry)
    art = from_mutated(path, buggy, mutated, moved[0])
    art.origins = moved
    into.append(art)
    gate = next((o["gate_line"] for o in moved if o.get("gate_line") is not None), None)
    counterpart = {"aid": art.aid, "path": path, **({"gate_line": gate} if gate is not None else {})}
    return {"exists": "transplant", "parent": [counterpart]}


def change(original: bytes, start: int, end: int, replacement: str) -> tuple[tuple[str, ...], ...]:
    """What an edit changes, as tokens of its touched lines with whitespace ignored: per changed run its left
    neighbour, the tokens removed, the tokens added and its right neighbour."""
    first = original.count(b"\n", 0, start) + 1
    last = max(first, original.count(b"\n", 0, max(start, end - 1)) + 1)
    mutated = original[:start] + replacement.encode("utf-8") + original[end:]
    before, after = original.split(b"\n"), mutated.split(b"\n")
    old = _TOKEN.findall(b" ".join(before[first - 1 : last]).decode("utf-8", "replace"))
    new = _TOKEN.findall(b" ".join(after[first - 1 : last + len(after) - len(before)]).decode("utf-8", "replace"))
    runs = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            left = old[i1 - 1] if i1 else ""
            right = old[i2] if i2 < len(old) else ""
            runs.append((left, *old[i1:i2], "->", *new[j1:j2], right))
    return tuple(runs)


def _bare(signature: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
    """A change signature without the neighbouring tokens."""
    return tuple(run[1:-1] for run in signature)


def _generated(
    path: str,
    fixed: bytes,
    buggy: bytes,
    edit: dict[str, Any],
    file_hunks: list[fixlines.Hunk],
    candidates: list[dict[str, Any]],
    into: list[Artifact],
    witness: str,
) -> dict[str, Any]:
    """The tool's mutants of the buggy file that make the witness's change (the same tokens removed and added) on
    the old side of the hunks the witness touches, or on the buggy lines its unchanged touched lines map to; where
    several make it, those that also have the same neighbouring tokens, when any has."""
    touched = fixlines.touched_lines(fixed, edit["start"], edit["end"], edit["replacement"])
    places: set[int] = set()
    for hunk in file_hunks:
        if any(line in hunk.new_lines for line in touched):
            places.update(range(hunk.old_start, hunk.old_start + max(hunk.old_count, 1)))
    places.update(m for m in (fixlines.to_parent_line(line, file_hunks) for line in touched) if m is not None)
    wanted = change(fixed, edit["start"], edit["end"], edit["replacement"])
    matches = []
    for row in candidates:
        if row["path"] != path:
            continue
        e = row["edit"]
        lines = fixlines.touched_lines(buggy, e["start"], e["end"], e["replacement"])
        if not lines <= places:
            continue
        signature = change(buggy, e["start"], e["end"], e["replacement"])
        if _bare(signature) == _bare(wanted):
            matches.append((signature == wanted, row))
    if any(exact for exact, _ in matches):
        matches = [(exact, row) for exact, row in matches if exact]
    found = []
    for _, row in matches:
        art = Artifact.from_json(row)
        art.origins = [{**o, "witness": witness, "via": "generated"} for o in art.origins]
        into.append(art)
        gate = next((o.get("gate_line") for o in art.origins if o.get("gate_line") is not None), None)
        found.append({"aid": art.aid, "path": path, **({"gate_line": gate} if gate is not None else {})})
    return {"exists": "generated", "parent": found} if found else {"exists": "absent"}


def _buggy_side(directory: Path) -> dict[str, list[dict[str, Any]] | None]:
    """The tools' mutants of the buggy files per tool whose buggy-side generation ran (None: it failed), without
    the emissions outside the tool's default row (``metrics.DEFAULT_DROP``)."""
    found: dict[str, list[dict[str, Any]] | None] = {}
    for ledger in sorted((directory / "buggy").glob("*.ledger.json")):
        tool = ledger.name.removesuffix(".ledger.json")
        if json.loads(ledger.read_text(encoding="utf-8")).get("failed"):
            found[tool] = None
            continue
        drop = DEFAULT_DROP.get(tool, "")
        rows = [
            json.loads(line)
            for line in ledger.with_name(f"{tool}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        found[tool] = [
            row
            for row in rows
            if not (drop and any(str(o.get("native_id", "")).startswith(drop) for o in row.get("origins", [])))
        ]
    return found
