"""Adjudication of the auto-admitted candidates: is each one a fault fix, by a written rubric?

Two raters label every candidate independently and blind to every tool result and to each other: rater B (Claude
Opus agents) and rater C (Claude Sonnet agents). Each sees the same packet per candidate — the commit message, the
source diff, the test diff and how the fail-to-pass tests fail on the buggy tree — and the rubric below. Agreement
on ``bug`` admits; agreement on anything else rejects; a disagreement goes to a third rater of another model
family (a GPT model through the codex CLI), whose vote decides. For every selected bug a Claude agent marks, under a
second rubric, which fail-to-pass tests check the fault itself (the purity labels).

    python3 -m pymutbench.review build --per-subject 25     # packets in batches
    python3 -m pymutbench.review kappa                      # agreement of the two raters
    python3 -m pymutbench.review finalize                   # final.jsonl (rater agreement + adjudications)

A human audit of the labels samples the corpus (``human-sample``: every bug the third rater decided, the rest
drawn per project), gives two people the same packets without any model label or tool result, and compares
their labels with each other and with the models' (``human-agreement``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.orchestrate import OUT, WORK
from pymutbench.subjects import SUBJECTS

FAULT_CLASSES = (
    "comparison_or_boundary",
    "arithmetic_or_operator",
    "wrong_literal_or_constant",
    "missing_or_wrong_condition",
    "missing_statement_or_call",
    "wrong_variable_or_value",
    "wrong_return_value",
    "exception_handling",
    "type_or_none_handling",
    "string_format_or_regex",
    "call_arguments_or_api_use",
    "algorithm_or_data_structure",
    "other",
)

RUBRIC = f"""You are adjudicating candidates for a benchmark of real software faults (like Defects4J or
SWE-bench Verified). Each candidate is a commit of an open-source Python library that changes source code and
tests; its fail-to-pass tests pass on the fixed code and fail on the code before the commit.

Label each candidate with exactly one of:
- "bug": the commit repairs a FAULT — the code before the commit violated a behaviour that was already
  expected: documented behaviour, an existing API promise, a regression of previously working behaviour, a crash
  or wrong result on valid input, or an invariant any user would assume. The fail-to-pass tests check that
  behaviour.
- "feature": the commit adds new capability (a new function, option, parameter, supported input or output
  format) and the tests fail before it only because the capability did not exist yet.
- "behavior_change": a deliberate change of behaviour that was not a fault before (a policy change, a new
  default, a deprecation, a message rewording, an enhancement of an error message, a refactoring with test
  updates).
- "unclear": the packet does not let you decide.

Rules: judge the intent of the change from the code, the tests and the message together; a commit message
saying "fix" is evidence, not proof. Error-message text changes are "behavior_change" unless the old message
was wrong or missing. A fix that is mixed with unrelated features is still "bug" if the fail-to-pass tests check
the fault. Then classify the fault (for "bug" only, else "none") into one of: {", ".join(FAULT_CLASSES)}.
Give a one-sentence description of the fault in your own words, the evidence of the pre-existing contract, and
your confidence (high, medium or low).
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "fault_class", "description", "contract", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string", "enum": ["bug", "feature", "behavior_change", "unclear"]},
                    "fault_class": {"type": "string", "enum": [*FAULT_CLASSES, "none"]},
                    "description": {"type": "string"},
                    "contract": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}


# The purity rubric: which fail-to-pass tests of a selected bug check the fault itself (only those can make a
# witness). One Claude agent per batch of packets labels it, in PURITY_SCHEMA, into ``review/purity/``.
PURITY_RUBRIC = """Each candidate is a validated bug-fix commit of a Python library: its fail-to-pass tests (the keys of
fail_to_pass_on_buggy_code) pass on the fixed code and fail on the code before the commit. Some commits fix a
fault and also add a feature or change other behaviour in the same change.

For each candidate decide:
- pure_fix: true when the commit only repairs the fault (refactoring, documentation and test-only changes do not
  count against it); false when it also adds capability or changes other behaviour.
- fault_tests: exactly the fail-to-pass test ids (copied verbatim from the packet) that check the FAULT. For a
  pure fix this is every fail-to-pass test. For a mixed commit, leave out the tests that only exercise the new
  capability or the other behaviour change. Never invent ids. If no test checks the fault, return an empty list.
- note: one sentence explaining the split (empty for a pure fix).
"""

PURITY_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "pure_fix", "fault_tests", "note"],
                "properties": {
                    "id": {"type": "string"},
                    "pure_fix": {"type": "boolean"},
                    "fault_tests": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
            },
        }
    },
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def packet(row: dict[str, Any]) -> dict[str, Any]:
    repo = WORK / "repos" / row["subject"]
    message = _git(repo, "log", "-1", "--format=%B", row["commit"]).strip()
    tests = row.get("test_files", [])
    test_diff = _git(repo, "diff", "--no-renames", "-U3", row["parent"], row["commit"], "--", *tests) if tests else ""
    failures = {n: row["fail_to_pass_all"][n] for n in row.get("t_fix", [])}
    return {
        "id": f"{row['subject']}@{row['commit'][:12]}",
        "subject": row["subject"],
        "repository": SUBJECTS[row["subject"]].repo,
        "date": row["date"][:10],
        "commit_message": message[:3000],
        "source_diff": row.get("src_diff", "")[:8000],
        "test_diff": test_diff[:8000],
        "fail_to_pass_on_buggy_code": failures,
    }


def build(per_subject: int, batch_size: int, validate_dir: Path, inputs_dir: str = "inputs",
          subjects: list[str] | None = None) -> None:
    rows: list[dict[str, Any]] = []
    for name in subjects or sorted(SUBJECTS):
        path = validate_dir / f"{name}.jsonl"
        if not path.exists():
            continue
        listing = validate_dir / f"{name}.candidates.jsonl"
        if not listing.exists():
            raise SystemExit(f"{name}: no candidate list ({listing}): run validate-all --list")
        # Mainline order, newest first: the order of validation and of the corpus selection.
        lines = listing.read_text(encoding="utf-8").splitlines()
        order = {json.loads(line)["commit"]: i for i, line in enumerate(lines)}
        admitted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        admitted = sorted((r for r in admitted if r.get("admitted")), key=lambda r: order[r["commit"]])
        rows += admitted[:per_subject]
    done = set()
    review = OUT / "review"
    for rater in ("codex", "claude"):
        for labels in (review / rater).glob("batch-*.json"):
            done |= {item["id"] for item in json.loads(labels.read_text(encoding="utf-8")).get("labels", [])}
    for packed in review.glob("inputs*/batch-*.json"):  # already packed for rating, possibly still pending
        done |= {item["id"] for item in json.loads(packed.read_text(encoding="utf-8"))}
    rows = [row for row in rows if f"{row['subject']}@{row['commit'][:12]}" not in done]
    packets = [packet(row) for row in rows]
    inputs = review / inputs_dir
    inputs.mkdir(parents=True, exist_ok=True)
    start = len(list(inputs.glob("batch-*.json"))) if inputs.exists() else 0
    for offset in range(0, len(packets), batch_size):
        batch = packets[offset : offset + batch_size]
        (inputs / f"batch-{start + offset // batch_size:03d}.json").write_text(
            json.dumps(batch, indent=1, ensure_ascii=False), encoding="utf-8")
    (review / "rubric.md").write_text(RUBRIC, encoding="utf-8")
    (review / "schema.json").write_text(json.dumps(SCHEMA, indent=1), encoding="utf-8")
    print(f"{len(packets)} packets in {(len(packets) + batch_size - 1) // batch_size} batches")


def build_purity(batch_size: int) -> None:
    """Purity packets (the adjudication packet) for every selected bug without a purity label yet, in
    ``review/purity_inputs/``, with ``purity_rubric.md`` and ``purity_schema.json`` beside them."""
    review = OUT / "review"
    done = {item["id"] for labels in (review / "purity").glob("batch-*.json")
            for item in json.loads(labels.read_text(encoding="utf-8")).get("items", [])}
    rows = [json.loads(line) for path in sorted((OUT / "bugs").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    packets = [packet(row) for row in rows if f"{row['subject']}@{row['commit'][:12]}" not in done]
    inputs = review / "purity_inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    start = len(list(inputs.glob("batch-*.json")))
    for offset in range(0, len(packets), batch_size):
        (inputs / f"batch-{start + offset // batch_size:03d}.json").write_text(
            json.dumps(packets[offset : offset + batch_size], indent=1, ensure_ascii=False), encoding="utf-8")
    (review / "purity_rubric.md").write_text(PURITY_RUBRIC, encoding="utf-8")
    (review / "purity_schema.json").write_text(json.dumps(PURITY_SCHEMA, indent=1), encoding="utf-8")
    print(f"{len(packets)} purity packets in {(len(packets) + batch_size - 1) // batch_size} batches")


def rate_codex(model: str, effort: str, parallel: int, inputs: str, rater: str = "codex", inline: bool = False) -> None:
    """A codex rater: one read-only session per batch, answering in the label schema, into ``review/<rater>``."""
    review = OUT / "review"
    (review / rater).mkdir(parents=True, exist_ok=True)

    def one(batch: Path) -> str:
        target = review / rater / batch.name
        if target.exists():
            return f"{batch.name}: done"
        base = ["codex", "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-s", "read-only",
                "--skip-git-repo-check", "--ephemeral", "-C", str(review), "--output-schema",
                str(review / "schema.json"), "-o", str(target)]
        if inline:
            # The rubric and the packets in the prompt itself: the session needs no tool call.
            prompt = (RUBRIC + "\n\nLabel every candidate below by the rubric, one entry per candidate id, in the "
                      "given JSON schema. Use only these packet contents; do not run commands, browse or look up "
                      "repositories.\n\nCANDIDATES (JSON):\n" + batch.read_text(encoding="utf-8"))
            proc = subprocess.run([*base, "-"], input=prompt, capture_output=True, text=True, check=False,
                                  timeout=3600)
        else:
            prompt = (f"Read rubric.md and {inputs}/{batch.name} in the working directory. Label every candidate of "
                      "that batch by the rubric, one entry per candidate id, in the given JSON schema. Use only the "
                      "packet contents; do not browse, run code or look up the repositories.")
            proc = subprocess.run(
                [*base, prompt],
                stdin=subprocess.DEVNULL,  # codex exec appends a non-terminal stdin to the prompt, waiting for EOF
                capture_output=True, text=True, check=False, timeout=3600,
            )
        return f"{batch.name}: rc={proc.returncode}"

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(parallel) as pool:
        for line in pool.map(one, sorted((review / inputs).glob("batch-*.json"))):
            print(line, flush=True)


def _labels(rater: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted((OUT / "review" / rater).glob("batch-*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("labels", []):
            found[item["id"]] = item
    return found


RATER_A = "codex_medium"  # GPT-6-Astra, medium effort, every candidate (codex/ holds the high-effort subset)


def kappa(a_dir: str = RATER_A, b_dir: str = "claude") -> dict[str, Any]:
    a, b = _labels(a_dir), _labels(b_dir)
    common = sorted(set(a) & set(b))
    pairs = [(a[i]["label"] == "bug", b[i]["label"] == "bug") for i in common]
    n = len(pairs)
    agree = sum(x == y for x, y in pairs)
    pa = sum(x for x, _ in pairs) / n if n else 0.0
    pb = sum(y for _, y in pairs) / n if n else 0.0
    expected = pa * pb + (1 - pa) * (1 - pb)
    observed = agree / n if n else 0.0
    value = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    report = {"n": n, "agree": agree, "observed": round(observed, 3), "kappa": round(value, 3),
              "codex_bug": sum(x for x, _ in pairs), "claude_bug": sum(y for _, y in pairs),
              "disagreements": [i for i, (x, y) in zip(common, pairs, strict=True) if x != y]}
    print(json.dumps({k: v for k, v in report.items() if k != "disagreements"}))
    print(len(report["disagreements"]), "disagreements")
    return report


def build_tiebreak(batch_size: int) -> None:
    """Packets of the candidates the two primary raters disagree on (bug vs not bug), for the third rater."""
    split = set(disagreements())
    review = OUT / "review"
    target = review / "inputs_tiebreak"
    target.mkdir(parents=True, exist_ok=True)
    existing = sorted(target.glob("batch-*.json"))
    seen = {packet_["id"] for path in existing for packet_ in json.loads(path.read_text(encoding="utf-8"))}
    packets = []
    for path in sorted(review.glob("inputs*/batch-*.json")):
        if path.parent == target:
            continue
        for packet_ in json.loads(path.read_text(encoding="utf-8")):
            if packet_["id"] in split and packet_["id"] not in seen:
                seen.add(packet_["id"])
                packets.append(packet_)
    for offset in range(0, len(packets), batch_size):  # numbered after the batches already packed
        (target / f"batch-{len(existing) + offset // batch_size:03d}.json").write_text(
            json.dumps(packets[offset : offset + batch_size], indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(packets)} new disagreements in {(len(packets) + batch_size - 1) // batch_size} batches")


RATERS = {"B": "claude", "C": "claude_c"}  # the two primary raters: Claude Opus agents, Claude Sonnet agents
TIEBREAK = "tiebreak"  # GPT-6-Astra (codex, medium effort) on the candidates B and C disagree on


def _kappa(x: dict[str, dict[str, Any]], y: dict[str, dict[str, Any]]) -> dict[str, Any]:
    common = sorted(set(x) & set(y))
    pairs = [(x[i]["label"] == "bug", y[i]["label"] == "bug") for i in common]
    n = len(pairs)
    if not n:
        return {"n": 0}
    observed = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    return {"n": n, "agreement": round(observed, 3),
            "kappa": round((observed - expected) / (1 - expected), 3) if expected < 1 else 1.0}


def disagreements() -> list[str]:
    b, c = _labels(RATERS["B"]), _labels(RATERS["C"])
    return sorted(i for i in set(b) & set(c) if (b[i]["label"] == "bug") != (c[i]["label"] == "bug"))


def finalize() -> None:
    """B and C agreeing decides; a disagreement is decided by the third rater of another model family."""
    b, c, t = _labels(RATERS["B"]), _labels(RATERS["C"]), _labels(TIEBREAK)
    rows = []
    for cid in sorted(set(b) & set(c)):
        votes = {"B": b[cid], "C": c[cid]}
        if (b[cid]["label"] == "bug") != (c[cid]["label"] == "bug"):
            if cid in t:
                votes["T"] = t[cid]
            else:
                rows.append({"id": cid, "label": "pending", "votes": {k: v["label"] for k, v in votes.items()}})
                continue
        bug_votes = [name for name, item in votes.items() if item["label"] == "bug"]
        admitted = len(bug_votes) * 2 > len(votes)
        classes = Counter(votes[name]["fault_class"] for name in bug_votes)
        fault = classes.most_common(1)[0][0] if classes else "none"
        subject, _, commit12 = cid.partition("@")
        rows.append({"id": cid, "subject": subject, "commit12": commit12, "label": "bug" if admitted else "rejected",
                     "votes": {name: item["label"] for name, item in votes.items()}, "fault_class": fault,
                     "raters": votes})
    with (OUT / "review" / "final.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    gpt = {**_labels("codex_medium"), **_labels("codex")}  # every GPT-6-Astra label, for cross-family agreement
    agreement = {"B-C": _kappa(b, c), "GPT-B": _kappa(gpt, b), "GPT-C": _kappa(gpt, c),
                 "fault_class_B-C": _class_agreement(b, c)}
    (OUT / "review" / "agreement.json").write_text(json.dumps(agreement, indent=1), encoding="utf-8")
    print(f"{sum(r['label'] == 'bug' for r in rows)} admitted, {sum(r['label'] == 'pending' for r in rows)} pending, "
          f"of {len(rows)}; agreement {json.dumps(agreement)}")


def _class_agreement(x: dict[str, dict[str, Any]], y: dict[str, dict[str, Any]]) -> dict[str, Any]:
    both = [i for i in set(x) & set(y) if x[i]["label"] == "bug" and y[i]["label"] == "bug"]
    same = sum(1 for i in both if x[i]["fault_class"] == y[i]["fault_class"])
    return {"n": len(both), "same_class": same}


HUMAN_SEED = 20261111


def human_sample(size: int) -> None:
    """The audit sample: every corpus bug the third rater decided, then bugs drawn per project in proportion to
    its share of the corpus (seeded) until ``size``; per bug the rater packet and the fail-to-pass tests, without any
    model label or tool result (``review/human/packets``), and an empty label file per rater."""
    import random

    final = {row["id"]: row for row in _jsonl(OUT / "review" / "final.jsonl")}
    corpus: dict[str, dict[str, Any]] = {}
    for name in sorted(SUBJECTS):
        for row in _jsonl(OUT / "bugs" / f"{name}.jsonl"):
            corpus[f"{name}@{row['commit'][:12]}"] = row
    decided = sorted(i for i in corpus if final[i]["votes"].get("B") != final[i]["votes"].get("C"))
    rng = random.Random(HUMAN_SEED)
    rest = sorted(set(corpus) - set(decided))
    by_project: dict[str, list[str]] = {}
    for bug in rest:
        by_project.setdefault(bug.split("@")[0], []).append(bug)
    quota = max(0, size - len(decided))
    chosen = list(decided)
    shares = {p: len(v) / len(rest) * quota for p, v in by_project.items()}
    for project, bugs in sorted(by_project.items()):
        chosen += rng.sample(bugs, min(len(bugs), int(shares[project])))
    remaining = sorted(set(rest) - set(chosen))
    chosen += rng.sample(remaining, max(0, size - len(chosen)))
    human = OUT / "review" / "human"
    (human / "packets").mkdir(parents=True, exist_ok=True)
    for bug in sorted(chosen):
        packet_ = packet(corpus[bug])
        packet_["fail_to_pass_tests"] = sorted(corpus[bug].get("t_fix", []))
        (human / "packets" / f"{bug}.json").write_text(json.dumps(packet_, indent=1), encoding="utf-8")
    (human / "rubric.md").write_text(RUBRIC + "\n\n" + PURITY_RUBRIC, encoding="utf-8")
    (human / "sample.json").write_text(json.dumps({"seed": HUMAN_SEED, "size": len(chosen), "third_rater": decided,
                                                   "bugs": sorted(chosen)}, indent=1), encoding="utf-8")
    for rater in ("human_1", "human_2"):
        (human / rater).mkdir(exist_ok=True)
        (human / rater / "labels.jsonl").touch()
    print(f"{len(chosen)} bugs ({len(decided)} decided by the third rater) in {human}")


def human_agreement() -> dict[str, Any]:
    """Agreement of the two people with each other and with the final model labels on bug versus not-bug, with
    Wilson intervals of the raw agreement and the confusion matrix against the final labels; and, for the fault
    tests, the share of bugs whose two human sets equal the purity label's."""
    from pymutbench.stats import wilson

    human = OUT / "review" / "human"
    raters = {rater: {row["id"]: row for row in _jsonl(human / rater / "labels.jsonl")}
              for rater in ("human_1", "human_2")}
    final = {row["id"]: row for row in _jsonl(OUT / "review" / "final.jsonl")}
    batches = sorted((OUT / "review" / "purity").glob("batch-*.json"))
    purity = {item["id"]: set(item.get("fault_tests", [])) for path in batches
              for item in json.loads(path.read_text(encoding="utf-8")).get("items", [])}
    out: dict[str, Any] = {"labelled": {r: len(v) for r, v in raters.items()}}
    pairs = {"human_1 vs human_2": (raters["human_1"], raters["human_2"]),
             "human_1 vs models": (raters["human_1"], final), "human_2 vs models": (raters["human_2"], final)}
    for name, (x, y) in pairs.items():
        result = _kappa(x, y)
        if result["n"]:
            agree = round(result["agreement"] * result["n"])
            result["agreement_ci"] = [round(v, 3) for v in wilson(agree, result["n"])]
        out[name] = result
    for rater, labels in raters.items():
        matrix = Counter(f"{labels[i]['label'] == 'bug'} / {final[i]['label'] == 'bug'}" for i in labels if i in final)
        out[f"{rater} (human / models: is a bug)"] = dict(matrix)
        same = [i for i in labels if i in purity and "fault_tests" in labels[i]]
        out[f"{rater} fault tests equal to the purity label"] = (
            f"{sum(1 for i in same if set(labels[i]['fault_tests']) == purity[i])}/{len(same)}")
    (human / "agreement.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return out


SURVIVOR_RUBRIC = """Each packet is a mutant of a Python library's fixed source that the project's historical test suite
does not kill and that no test of the bug's fix kills either (a survivor that is not a witness). Label it:

* equivalent: no input can tell the mutant from the original program (the change has no observable effect);
* test_gap: a test could kill it, and a developer would want that test (a real gap of the suite);
* uninteresting: a test could kill it, but the behaviour it changes is not worth a test (logging, messages,
  defensive branches, performance-only code).

Write one JSON line per packet: {"id": "<packet id>", "label": "<label>", "note": "<one sentence>"}.
"""


def survivor_sample(per_tool: int) -> None:
    """Per tool, ``per_tool`` of its unique survivors that are not witnesses, drawn with a seed over the corpus;
    each packet shows the mutant as a diff of its file, without the tool's name or any verdict but survival, for a
    person to label (``SURVIVOR_RUBRIC``): the share of noise among the survivors S(k) draws from."""
    import difflib
    import random

    from pymutbench import fixes
    from pymutbench.metrics import TOOLS

    by_tool: dict[str, list[tuple[str, dict[str, Any]]]] = {tool: [] for tool in TOOLS}
    for name in sorted(SUBJECTS):
        for bug in _jsonl(OUT / "bugs" / f"{name}.jsonl"):
            directory = OUT / "gen" / name / bug["commit"][:12]
            verdicts = {r["aid"]: r for r in _jsonl(directory / "oracle.jsonl") if "aid" in r}
            for tool in TOOLS:
                seen = set()
                for row in _jsonl(directory / f"{tool}.jsonl"):
                    verdict = verdicts.get(row["aid"], {})
                    if row["aid"] in seen or verdict.get("status") != "survived" or verdict.get("witness") is not False:
                        continue
                    seen.add(row["aid"])
                    by_tool[tool].append((f"{name}@{bug['commit'][:12]}", row))
    rng = random.Random(HUMAN_SEED)
    folder = OUT / "review" / "survivors"
    (folder / "packets").mkdir(parents=True, exist_ok=True)
    key: dict[str, dict[str, Any]] = {}  # packet -> the tools whose sample drew it, and the artifact
    for tool, items in by_tool.items():
        for bug, row in rng.sample(sorted(items, key=lambda x: (x[0], x[1]["aid"])), min(per_tool, len(items))):
            subject, commit12 = bug.split("@")
            fixed = fixes.load(OUT / "gen" / subject / commit12)["files"][row["path"]]["fixed"]
            edit = row["edit"]
            raw = fixed.encode("utf-8")
            mutated = (raw[: edit["start"]] + edit["replacement"].encode("utf-8") + raw[edit["end"] :]).decode("utf-8")
            diff = "".join(difflib.unified_diff(fixed.splitlines(keepends=True), mutated.splitlines(keepends=True),
                                                row["path"], row["path"] + " (mutant)", n=6))
            packet_id = f"{bug}#{row['aid'][:12]}"
            key.setdefault(packet_id, {"tools": [], "aid": row["aid"]})["tools"].append(tool)
            packet_ = {"id": packet_id, "bug": bug, "path": row["path"], "diff": diff}
            (folder / "packets" / f"{packet_id.replace('/', '_')}.json").write_text(json.dumps(packet_, indent=1),
                                                                                   encoding="utf-8")
    (folder / "rubric.md").write_text(SURVIVOR_RUBRIC, encoding="utf-8")
    (folder / "key.json").write_text(json.dumps(key, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{len(key)} packets in {folder}")


def survivor_labels() -> dict[str, Any]:
    """The labelled survivors per tool: counts of each label (``review/survivors/labels.jsonl``)."""
    folder = OUT / "review" / "survivors"
    key = json.loads((folder / "key.json").read_text(encoding="utf-8"))
    out: dict[str, Counter[str]] = {}
    for row in _jsonl(folder / "labels.jsonl"):
        for tool in key[row["id"]]["tools"]:
            out.setdefault(tool, Counter())[row["label"]] += 1
    summary = {tool: dict(sorted(counts.items())) for tool, counts in sorted(out.items())}
    (folder / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return summary


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("--per-subject", type=int, default=25)
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--validate-dir", default=str(OUT / "validate"))
    p.add_argument("--inputs-dir", default="inputs", help="the batch directory under review/")
    p.add_argument("--subjects", default="", help="comma-separated subjects (default: all)")
    p = sub.add_parser("rate-codex")
    p.add_argument("--model", default="gpt-6-astra")
    p.add_argument("--effort", default="high")
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--inputs", default="inputs", help="the batch directory under review/")
    p.add_argument("--rater", default="codex", help="the output directory under review/")
    p.add_argument("--inline", action="store_true", help="send the packets in the prompt (no file reads)")
    p = sub.add_parser("build-tiebreak")
    p.add_argument("--batch-size", type=int, default=5)
    p = sub.add_parser("build-purity", help="purity packets for the selected bugs not labelled yet")
    p.add_argument("--batch-size", type=int, default=20)
    sub.add_parser("kappa")
    p = sub.add_parser("human-sample", help="the sample and blind packets of the human label audit")
    p.add_argument("--size", type=int, default=100)
    sub.add_parser("human-agreement", help="the human audit's agreement with each other and with the models")
    p = sub.add_parser("survivor-sample", help="packets of surviving non-witness mutants for a manual noise label")
    p.add_argument("--per-tool", type=int, default=50)
    sub.add_parser("survivor-labels", help="the labelled survivors per tool")
    sub.add_parser("finalize")
    args = parser.parse_args()
    match args.cmd:
        case "build":
            build(args.per_subject, args.batch_size, Path(args.validate_dir), args.inputs_dir,
                  [s for s in args.subjects.split(",") if s] or None)
        case "rate-codex":
            rate_codex(args.model, args.effort, args.parallel, args.inputs, args.rater, args.inline)
        case "build-tiebreak":
            build_tiebreak(args.batch_size)
        case "build-purity":
            build_purity(args.batch_size)
        case "kappa":
            kappa()
        case "finalize":
            finalize()
        case "human-sample":
            human_sample(args.size)
        case "human-agreement":
            human_agreement()
        case "survivor-sample":
            survivor_sample(args.per_tool)
        case "survivor-labels":
            survivor_labels()
        case _:
            raise AssertionError(args.cmd)


if __name__ == "__main__":
    main()
