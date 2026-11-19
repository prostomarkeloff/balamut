"""Rebuild the working outputs of a published release from its data (``results/``): the inverse of
``report.export``.

    python3 -m pymutbench.restore --results bench/results

writes into ``$PMB_OUT_DIR`` (``$PMB_WORK/out``) everything ``report bugs`` reads: the selection and its bug
records, the adjudications and purity labels, and per bug its scope, fix record, union of artifacts, every tool's
artifacts (one record per origin of the tool, as its generation wrote them), every generation ledger, every
oracle verdict and re-judgement, and the buggy-side counterparts with their verdicts. ``report bugs`` on the
restored outputs gives the published ``bug_track.json``; judging a new tool's mutants on top of them needs the
subjects' clones and images (``bench/README.md``).
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.metrics import TOOLS
from pymutbench.orchestrate import OUT
from pymutbench.report import DATA_FILES


def _records(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(record: dict[str, Any]) -> dict[str, Any]:
    """A published record as the harness wrote it: the bug key dropped, a meta record's ``commit`` back as ``bug``."""
    row = {key: value for key, value in record.items() if key != "bug"}
    if row.get("kind") == "meta" and "commit" in row:
        row["bug"] = row.pop("commit")
    return row


def restore(results: Path, out: Path) -> int:
    data = results / "data"
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty: restore writes into an empty output directory")
    for name in ("selection.json", "sensitivity.json", "equivalence.json"):
        if (results / name).exists():
            (out / "bugs").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(results / name, out / "bugs" / name)
    review = out / "review"
    (review / "purity").mkdir(parents=True, exist_ok=True)
    for name in ("final.jsonl", "agreement.json", "purity_rubric.md", "purity_schema.json"):
        shutil.copyfile(results / name, review / name)
    items = [
        json.loads(line) for line in (results / "purity.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    (review / "purity" / "batch-000.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    by_subject: dict[str, list[dict[str, Any]]] = {}
    bugs = 0
    for record in _records(data / "generation.jsonl.gz"):
        subject, commit12 = record["bug"].split("@")
        directory = out / "gen" / subject / commit12
        if "record" in record:
            bugs += 1
            directory.mkdir(parents=True, exist_ok=True)
            by_subject.setdefault(subject, []).append(record["record"])
            (directory / "bug.json").write_text(json.dumps(record["record"], ensure_ascii=False), encoding="utf-8")
            (directory / "scope.json").write_text(json.dumps(record["scope"]), encoding="utf-8")
            (directory / "fix.json").write_text(json.dumps(record["fix"]), encoding="utf-8")
            if record.get("prefix") is not None:
                (directory / "prefix.json").write_text(json.dumps(record["prefix"], sort_keys=True), encoding="utf-8")
        else:
            folder = directory / "buggy" if record.get("side") == "buggy" else directory
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{record['tool']}.ledger.json").write_text(
                json.dumps(record["ledger"], indent=1), encoding="utf-8"
            )
    for subject, records in by_subject.items():
        _write(out / "bugs" / f"{subject}.jsonl", records)
    for name, filename in DATA_FILES.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in _records(data / f"{name}.jsonl.gz"):
            grouped.setdefault(record["bug"], []).append(_row(record))
        for bug, rows in grouped.items():
            subject, commit12 = bug.split("@")
            _write(out / "gen" / subject / commit12 / filename, rows)
    # Every tool's own artifacts: one record per origin of the tool in the union, as its generation wrote them.
    for union in sorted((out / "gen").glob("*/*/union.jsonl")):
        artifacts = [json.loads(line) for line in union.read_text(encoding="utf-8").splitlines() if line.strip()]
        for tool in TOOLS:
            if not (union.parent / f"{tool}.ledger.json").exists():
                continue
            _write(
                union.parent / f"{tool}.jsonl",
                [
                    {**art, "origins": [origin]}
                    for art in artifacts
                    for origin in art["origins"]
                    if origin.get("tool") == tool
                ],
            )
    return bugs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True, help="a release's results directory (bench/results)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"restored {restore(Path(args.results), OUT)} bugs into {OUT}")


if __name__ == "__main__":
    main()
