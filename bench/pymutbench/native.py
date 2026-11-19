"""Native campaigns: each tool runs its own engine on the same files, as its documentation's quick start runs it.

Runs inside a native image (``orchestrate build-native``): the subject at HEAD in ``/work/run`` (an editable
install in every tool's environment) and one virtual environment per tool in ``/native/<tool>``. A campaign is
timed from the first command to the last (wall clock, CPU time of all waited-for processes, the peak resident
size of the largest one); its per-mutant verdicts are read from the tool's own report and each native mutant is
mapped to a normalized artifact through the same adapter the generation stage uses, so the oracle's verdict of
the identical mutated file can be compared with the tool's.

Verdicts are normalized to: killed, survived, timeout, no_coverage (the tool did not run any test), error
(the tool could not judge it), skipped (the tool chose not to run it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.artifacts import from_mutated
from pymutbench.astmat import materialize
from pymutbench.generate import TOOLS as GEN_TOOLS
from pymutbench.registry import REGISTRY

RUN = Path("/work/run")  # rebound by --root
ADAPTERS = Path(__file__).resolve().parent / "adapters"


@dataclass
class Ctx:
    tool: str  # the benchmark's tool name (the generation adapter's key)
    venv: str  # the directory of the tool's environment under /native
    files: list[str]  # scope files relative to RUN
    tests: list[str]  # test roots relative to RUN
    log: list[str] = field(default_factory=list)
    stated_error: str | None = None  # the error the tool's own report gives for the campaign, when it gives one
    limit: int = 7200  # seconds one command of the campaign may run (whole seconds: the timeout message shows it)

    @property
    def bin(self) -> Path:
        return Path(f"/native/{self.venv}/bin")

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "VIRTUAL_ENV": f"/native/{self.venv}"}

    def sh(self, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(argv, cwd=cwd or RUN, env=self.env(), capture_output=True, text=True,
                              timeout=self.limit, check=False)
        self.log.append(f"$ {' '.join(argv)}\nrc={proc.returncode}\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}")
        return proc


# --- the campaigns ---------------------------------------------------------------------------------------------


def c_balamut(ctx: Ctx) -> list[dict[str, Any]]:
    ctx.sh("balamut", "new")
    # The quick start: `balamut mutate PATHS` with the default kinds (src, concurrency, fault, oracle; oracle only
    # applies to test files, none of which is a campaign path).
    proc = ctx.sh("balamut", "mutate", *ctx.files, "--json", "--progress", "none")
    rows = json.loads(proc.stdout[proc.stdout.index("[") :]) if "[" in proc.stdout else []
    out = []
    for row in rows:
        verdict = {"killed": "killed", "survived": "survived", "timeout": "timeout", "no_coverage": "no_coverage",
                   "unsupported": "error"}.get(row["status"], "error")
        out.append({"native_id": row["key"], "native_status": row["status"], "verdict": verdict, "path": row["path"],
                    "edit": {"start": row["start_byte"], "end": row["end_byte"], "replacement": row["mutated"]}})
    return out


_MUTMUT_STATUS = {1: "killed", 3: "killed", 0: "survived", 5: "no_coverage", 33: "no_coverage", 34: "skipped",
                  36: "timeout", -24: "timeout", 24: "timeout", 152: "timeout", 255: "timeout", 37: "killed",
                  -11: "killed", -9: "killed"}


def c_mutmut(ctx: Ctx) -> list[dict[str, Any]]:
    # source_paths as mutmut guesses them in a checkout named after the project: `src` for a src layout, the
    # package directory otherwise (from /work/run it guesses nothing). mutmut copies them whole into mutants/ and
    # takes the original ones off sys.path, so every package the tests import must be among them (attrs's tests
    # import both `src/attr` and `src/attrs`). The measured files are only_mutate, the test roots test selection.
    pyproject = RUN / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    packages = sorted({"src" if Path(f).parts[0] == "src" else Path(f).parts[0] for f in ctx.files})
    pyproject.write_text(text + "\n[tool.mutmut]\n"
                         f"source_paths = {json.dumps(packages)}\n"
                         f"only_mutate = {json.dumps(ctx.files)}\n"
                         f"pytest_add_cli_args_test_selection = {json.dumps(ctx.tests)}\n", encoding="utf-8")
    ctx.sh("mutmut", "run")
    out = []
    for meta in (RUN / "mutants").rglob("*.meta"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        for key, code in data.get("exit_code_by_key", {}).items():
            out.append({"native_id": key.rpartition(".")[2], "native_status": code,
                        "verdict": _MUTMUT_STATUS.get(code, "error") if code is not None else "skipped",
                        "path": str(meta.relative_to(RUN / "mutants"))[: -len(".meta")]})
    return out


def c_cosmic_ray(ctx: Ctx) -> list[dict[str, Any]]:
    modules = ", ".join(json.dumps(f) for f in ctx.files)
    tests = " ".join(ctx.tests)
    (RUN / "cr.toml").write_text(
        "[cosmic-ray]\n"
        f"module-path = [{modules}]\n"
        "timeout = 60.0\n"
        "excluded-modules = []\n"
        f'test-command = "python -m pytest -x -q {tests}"\n\n'
        "[cosmic-ray.distributor]\n"
        'name = "local"\n',
        encoding="utf-8",
    )
    ctx.sh("cosmic-ray", "init", "cr.toml", "session.sqlite")
    ctx.sh("cosmic-ray", "baseline", "cr.toml")
    ctx.sh("cosmic-ray", "exec", "cr.toml", "session.sqlite")
    dump = ctx.sh("cosmic-ray", "dump", "session.sqlite")
    out = []
    for line in dump.stdout.splitlines():
        if not line.strip():
            continue
        item, result = json.loads(line)
        spec = item["mutations"][0]
        result = result or {}
        test, worker = result.get("test_outcome"), result.get("worker_outcome")
        if worker == "timeout":
            verdict = "timeout"
        elif worker not in (None, "normal"):
            verdict = "error"
        else:
            verdict = {"killed": "killed", "survived": "survived", "incompetent": "killed"}.get(test, "skipped")
        out.append({"native_id": f"{spec['operator_name']}#{spec['occurrence']}", "native_status": f"{worker}/{test}",
                    "verdict": verdict, "path": spec["module_path"]})
    return out


def _import_root(path: str) -> str:
    """The directory a scope file is imported from: the parent of its outermost package."""
    root = Path(path).parent
    while root != Path() and (RUN / root / "__init__.py").exists():
        root = root.parent
    return str(root)


def c_poodle(ctx: Ctx) -> list[dict[str, Any]]:
    # poodle's JSON report lists only the mutants the tests did not find unless `json_report_found` is set. Its
    # first-choice configuration file, poodle.toml (read ahead of pyproject.toml; no subject configures poodle),
    # sets it, so the report holds every trial.
    (RUN / "poodle.toml").write_text("[poodle.reporter_opts]\njson_report_found = true\n", encoding="utf-8")
    # poodle runs the tests with its copy of each source folder on PYTHONPATH, so a source folder is the directory
    # the package is imported from (`src`, or the root of a flat layout): given the package directory itself, the
    # tests import the unmutated package.
    roots = sorted({_import_root(f) for f in ctx.files})
    only = [arg for f in ctx.files for arg in ("--only", Path(f).name)]
    ctx.sh("poodle", *roots, *only, "--json", "poodle.json")
    data = json.loads((RUN / "poodle.json").read_text(encoding="utf-8")) if (RUN / "poodle.json").exists() else {}
    out = []
    for trial in data.get("mutant_trials", []):
        mutant, result = trial["mutant"], trial["result"]
        code = result.get("reason_code") or ""
        verdict = "killed" if result.get("found") else ("timeout" if "timeout" in code.lower() else
                                                        "error" if "error" in code.lower() else "survived")
        out.append({"native_id": f"{mutant['mutator_name']}@{mutant['lineno']}:{mutant['col_offset']}",
                    "native_status": code or ("found" if result.get("found") else ""), "verdict": verdict,
                    "path": mutant["source_file"], "poodle": mutant})
    return out


def c_gremlins(ctx: Ctx) -> list[dict[str, Any]]:
    ctx.sh(str(ctx.bin / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--gremlins",
           f"--gremlin-targets={','.join(ctx.files)}", "--gremlin-report=json", *ctx.tests)
    report = RUN / "coverage" / "gremlins" / "gremlins.json"
    data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    out = []
    for row in data.get("results", []):
        verdict = {"zapped": "killed", "survived": "survived", "timeout": "timeout", "pardoned": "skipped"}.get(
            row["status"], "error")
        out.append({"native_id": row["gremlin_id"], "native_status": row["status"], "verdict": verdict,
                    "path": os.path.relpath(row["file_path"], RUN)})
    return out


def c_leela(ctx: Ctx) -> list[dict[str, Any]]:
    targets = [arg for f in ctx.files for arg in ("--target", f)]
    ctx.sh(str(ctx.bin / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--leela", *targets,
           "--leela-html", "leela.html", *ctx.tests)
    html = RUN / "leela.html"
    if not html.exists():
        return []
    match = re.search(r"window\.LEELA_DATA = (\{.*?\});\n", html.read_text(encoding="utf-8"), re.DOTALL)
    data = json.loads(match.group(1)) if match else {}
    out = []
    for item in _leela_mutants(data):
        status = str(item.get("status", "")).lower()
        verdict = {"killed": "killed", "survived": "survived", "error": "error",
                   "timeout": "timeout"}.get(status, "error")
        match = [f for f in ctx.files if f == item["path"] or f.endswith("/" + item["path"])]
        out.append({"native_id": item["key"], "native_status": status, "verdict": verdict,
                    "path": match[0] if len(match) == 1 else None})
    return out


def _leela_mutants(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-mutant rows of leela's embedded report data: files keyed by base name, mutants with the engine's
    id (one counter across the target files), line, column, status."""
    return [{"key": str(m["id"]), "status": m.get("status"), "path": name}
            for name, entry in data.get("files", {}).items() for m in entry.get("mutants", [])]


def c_mutation(ctx: Ctx) -> list[dict[str, Any]]:
    include = [f"--include={f}" for f in ctx.files]
    ctx.sh("mutation", "play", *include, "--exclude=*test*", *ctx.tests)
    db_path = RUN / ".mutation.db"
    if not db_path.exists():
        return []
    db = sqlite3.connect(db_path)
    survivors = {bytes(uid) for (uid,) in db.execute("SELECT uid FROM results WHERE status = 0")}
    out = []
    for raw, path, diff in db.execute("SELECT uid, path, diff FROM mutations"):
        uid = bytes(raw)
        # The tool stores paths relative to the project root it ran in (absolute ones are resolved the same way).
        rel = os.path.relpath(Path(path) if Path(path).is_absolute() else RUN / path, RUN)
        out.append({"native_id": uid.hex(), "native_status": "survived" if uid in survivors else "killed",
                    "verdict": "survived" if uid in survivors else "killed", "path": rel,
                    "native_diff": zlib.decompress(diff).decode("utf-8")})
    return out


def c_universalmutator(ctx: Ctx) -> list[dict[str, Any]]:
    out = []
    tests = " ".join(ctx.tests)
    for rel in ctx.files:
        mutants = RUN / "um" / rel.replace("/", "__")
        mutants.mkdir(parents=True, exist_ok=True)
        ctx.sh("mutate", rel, "python", "--mutantDir", str(mutants))
        ctx.sh("analyze_mutants", rel, f"python -m pytest -x -q {tests}", "--mutantDir", str(mutants), cwd=RUN)
        killed = (RUN / "killed.txt").read_text(encoding="utf-8").split() if (RUN / "killed.txt").exists() else []
        alive = (RUN / "notkilled.txt").read_text(encoding="utf-8").split() if (RUN / "notkilled.txt").exists() else []
        out += [{"native_id": Path(name).name, "native_status": "killed", "verdict": "killed", "path": rel}
                for name in killed]
        out += [{"native_id": Path(name).name, "native_status": "notkilled", "verdict": "survived", "path": rel}
                for name in alive]
        for name in ("killed.txt", "notkilled.txt"):
            (RUN / name).unlink(missing_ok=True)
    return out


def c_fest(ctx: Ctx) -> list[dict[str, Any]]:
    sources = [arg for f in ctx.files for arg in ("-s", f)]
    proc = ctx.sh("fest", "run", *sources, "-o", "json")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for row in data.get("results", []):
        mutant = row["mutant"]
        status = str(row.get("status"))
        verdict = {"Killed": "killed", "Survived": "survived", "Timeout": "timeout", "NoCoverage": "no_coverage"}.get(
            status, "error")
        out.append({"native_id": f"{mutant['mutator_name']}@{mutant['line']}:{mutant['column']}",
                    "native_status": status,
                    "verdict": verdict, "path": os.path.relpath(mutant["file_path"], RUN),
                    "edit": {"start": mutant["byte_offset"], "end": mutant["byte_offset"] + mutant["byte_length"],
                             "replacement": mutant["mutated_text"]}})
    return out


def c_irradiate(ctx: Ctx) -> list[dict[str, Any]]:
    tests = ctx.tests[0] if ctx.tests else "tests"
    ctx.sh("irradiate", "run", *ctx.files, "--tests-dir", tests, "--python", str(ctx.bin / "python"),
           "--report", "json", "-o", "irradiate.json")
    report = RUN / "irradiate.json"
    data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    out = []
    for path, entry in data.get("files", {}).items():
        rel = next((f for f in ctx.files if f.endswith(path) or path.endswith(f)), None)
        source = (RUN / rel).read_text(encoding="utf-8") if rel else ""
        for m in entry.get("mutants", []):
            status = str(m.get("status"))
            verdict = {"Killed": "killed", "Survived": "survived", "Timeout": "timeout", "NoCoverage": "no_coverage",
                       "Ignored": "skipped"}.get(status, "error")
            row = {"native_id": m["id"].rpartition(".")[2], "native_status": status, "verdict": verdict, "path": rel}
            edit = _irradiate_edit(source, m) if rel and "location" in m and "replacement" in m else None
            if edit is not None:
                row["edit"] = edit
            out.append(row)
    return out


def _irradiate_edit(source: str, mutant: dict[str, Any]) -> dict[str, Any] | None:
    """The byte edit of an irradiate report entry, only when the text its description (``replaced `OLD` with
    `NEW```) says was replaced lies at the report's location, or where one of the report's systematic offsets
    puts it. A decorator removal is reported as an empty span at the start of the decorator's line, which its edit
    removes. A mutant in a method's signature is reported at the columns of the method's dedented variant, the
    method's indentation to the left. A source-patch mutant of a decorated function is reported as many lines
    higher as its decorators take. Any other entry gets no edit and stays unmapped."""
    described = re.fullmatch(r"replaced `(.*)` with `(.*)`", str(mutant.get("description", "")), re.DOTALL)
    if described is None:
        return None
    old = described.group(1).encode("utf-8")
    location = mutant["location"]
    edit = _stryker_edit(source, location, mutant["replacement"])
    data = source.encode("utf-8")
    if data[edit["start"] : edit["end"]] == old:
        return edit
    at_line_start = location["start"]["column"] == 1 and edit["start"] == edit["end"]
    if at_line_start and old.endswith(b"\n") and data[edit["start"] : edit["start"] + len(old)] == old:
        return {"start": edit["start"], "end": edit["start"] + len(old), "replacement": mutant["replacement"]}
    for lines, columns in _irradiate_offsets(source, str(mutant.get("id", "")).rpartition(".")[2], location):
        shifted = {end: {"line": location[end]["line"] + lines, "column": location[end]["column"] + columns}
                   for end in ("start", "end")}
        edit = _stryker_edit(source, shifted, mutant["replacement"])
        if data[edit["start"] : edit["end"]] == old:
            return edit
    return None


def _irradiate_offsets(source: str, native_id: str, location: dict[str, Any]) -> list[tuple[int, int]]:
    """(lines, columns) offsets of irradiate's report for an entry of the function its id names
    (``xǁClassǁname__…`` or ``x_name__…``): its indentation for a span in the signature of a method's variant, the
    lines of its decorators for a source-patch mutant."""
    import ast

    named = re.fullmatch(r"x(?:ǁ(?P<cls>[^ǁ]+)ǁ|_)(?P<name>.+?)__(?P<kind>irradiate|sp|decrem)_\d+", native_id)
    if named is None:
        return []
    tree = ast.parse(source)
    scopes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == named["cls"]] if named["cls"] \
        else [tree]
    functions = [n for scope in scopes for n in scope.body
                 if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == named["name"]]
    offsets = []
    for function in functions:
        if named["kind"] == "irradiate" and named["cls"] and function.lineno <= location["start"]["line"] < \
                function.body[0].lineno:
            offsets.append((0, function.col_offset))
        if named["kind"] == "sp" and function.decorator_list:
            offsets.append((function.lineno - min(d.lineno for d in function.decorator_list), 0))
    return offsets


def _stryker_edit(source: str, location: dict[str, Any], replacement: str) -> dict[str, Any]:
    """A location of irradiate's report (1-based lines and columns, end column exclusive) as a byte edit. Its
    columns count UTF-8 bytes, as the offsets of Python's AST do: on a line with non-ASCII text before the span, a
    column counted in characters lands past the replaced text."""
    lines = source.encode("utf-8").splitlines(keepends=True)

    def offset(point: dict[str, int]) -> int:
        return sum(len(x) for x in lines[: point["line"] - 1]) + point["column"] - 1

    return {"start": offset(location["start"]), "end": offset(location["end"]), "replacement": replacement}


def c_deployproof(ctx: Ctx) -> list[dict[str, Any]]:
    """`deployproof check --files ... --json`: the report lists survivors (file, line, description, mutated line)
    and counts; every other mutant of its deterministic enumeration was killed when the counts agree."""
    proc = ctx.sh("deployproof", "check", "--files", *ctx.files, "--tests", *ctx.tests, "--json")
    try:
        report = json.loads(proc.stdout[proc.stdout.index("{") :])
    except ValueError:
        return []
    testing = report.get("mutation_testing", {})
    # The report's own account of a baseline that failed (the campaign then judges no mutant); the campaign log
    # keeps only the end of the report, after it.
    ctx.stated_error = testing.get("collection_error") or None
    survivors =[(s["file"], s["line"], s["description"], " ".join(str(s.get("mutated_code", "")).split()))
                 for s in testing.get("surviving_mutants", [])]
    out = [{"native_id": f"survivor:{file}:{line}:{desc}", "native_status": "survived", "verdict": "survived",
            "path": file, "dp": {"line": line, "description": desc, "mutated_line": mutated}}
           for file, line, desc, mutated in survivors]
    out.append({"native_id": "<counts>", "native_status": json.dumps({k: testing.get(k) for k in (
        "total_mutants", "killed_mutants", "survived_mutants_count", "runner_errors_count", "valid_mutants_count")}),
                "verdict": "skipped"})
    return out


CAMPAIGNS: dict[str, Callable[[Ctx], list[dict[str, Any]]]] = {
    "balamut": c_balamut, "mutmut": c_mutmut, "cosmic-ray": c_cosmic_ray, "poodle": c_poodle,
    "pytest-gremlins": c_gremlins, "pytest-leela": c_leela, "mutation": c_mutation,
    "universalmutator": c_universalmutator, "fest": c_fest, "irradiate": c_irradiate, "deployproof": c_deployproof,
}
VENV = {name: tool.venv for name, tool in REGISTRY.items()}  # tool -> its environment under /native


# --- mapping native mutants to artifacts -----------------------------------------------------------------------


ARTIFACTS: dict[str, dict[str, Any]] = {}  # aid -> {"path", "edit"}: every artifact a native mutant maps to
OPERATORS: dict[tuple[str, str], str] = {}  # (path, native id) -> the adapter's description of that mutant


def adapter_artifacts(ctx: Ctx, pristine: Path) -> dict[tuple[str, str], str]:
    """(path, native id) -> artifact id, from the generation adapter run on the pristine files."""
    _, script = GEN_TOOLS[ctx.tool]
    out = Path("/tmp") / f"adapter-{ctx.tool}.jsonl"
    subprocess.run([str(ctx.bin / "python"), str(ADAPTERS / script), "--root", str(pristine), "--files",
                    ",".join(ctx.files), "--out", str(out)], cwd="/tmp", capture_output=True, check=False)
    mapping: dict[tuple[str, str], str] = {}
    if not out.exists():
        return mapping
    for line in out.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("status") != "ok":
            continue
        original = (pristine / record["path"]).read_bytes()
        art = from_mutated(record["path"], original, record["mutated"].encode("utf-8"), {})
        mapping[(record["path"], str(record["native_id"]))] = art.aid
        OPERATORS[(record["path"], str(record["native_id"]))] = str(record.get("operator", ""))
        ARTIFACTS[art.aid] = {"path": art.path, "edit": {"start": art.edit.start, "end": art.edit.end,
                                                         "replacement": art.edit.replacement}}
    return mapping


def _mutation_aid(row: dict[str, Any], pristine: Path, by_tree: dict[tuple[str, str], str]) -> str | None:
    """The artifact of a ``mutation`` record: its stored diff applied to the canonical text, as the tool does,
    matched to the adapter's artifact of the same mutated program (equal ASTs), else that program materialized as
    a verified edit of the file. The second path maps the number literals: the tool draws their random values in
    worker processes, which Python reseeds at every fork, so no seed reproduces the adapter's draws. A program
    with no faithful edit stays unmapped."""
    import ast

    sys.path.insert(0, "/native/mutation/lib/python3.14/site-packages")
    import mutation as tool

    original = (pristine / row["path"]).read_bytes()
    source = original.decode("utf-8")
    canonical = ast.unparse(ast.parse(source))
    try:
        tree = ast.parse(tool.patch(row["native_diff"], canonical))
    except Exception:
        return None
    aid = by_tree.get((row["path"], ast.dump(tree, include_attributes=False)))
    if aid is not None:
        return aid
    text, status = materialize(source, tree)
    if status != "ok" or text is None:
        return None
    art = from_mutated(row["path"], original, text.encode("utf-8"), {})
    ARTIFACTS[art.aid] = {"path": art.path, "edit": {"start": art.edit.start, "end": art.edit.end,
                                                     "replacement": art.edit.replacement}}
    return art.aid


def map_rows(ctx: Ctx, rows: list[dict[str, Any]], pristine: Path) -> None:
    # Native ids may embed the file's absolute path (pytest-gremlins hashes it): the adapter runs on the same
    # path, with the scope files restored to their pristine bytes first.
    for rel in ctx.files:
        shutil.copyfile(pristine / rel, RUN / rel)
    mapping = adapter_artifacts(ctx, RUN)
    by_id: dict[str, list[tuple[str, str]]] = {}
    for (path, native), aid in mapping.items():
        by_id.setdefault(native, []).append((path, aid))
    by_tree: dict[tuple[str, str], str] = {}
    if ctx.tool == "mutation":
        import ast

        for aid, data in ARTIFACTS.items():
            original = (pristine / data["path"]).read_bytes()
            e = data["edit"]
            mutated = original[: e["start"]] + e["replacement"].encode("utf-8") + original[e["end"] :]
            try:
                by_tree[(data["path"], ast.dump(ast.parse(mutated.decode("utf-8")), include_attributes=False))] = aid
            except SyntaxError:
                continue
    for row in rows:
        path = row.get("path")
        if ctx.tool == "balamut" and "balamut_" in row.get("edit", {}).get("replacement", ""):
            row["unrepresentable"] = "runtime_helper"  # judged by balamut with its injected helper only
            continue
        if ctx.tool == "mutation" and path and "native_diff" in row:
            row["aid"] = _mutation_aid(row, pristine, by_tree)
            continue
        if "edit" in row and path:
            original = (pristine / path).read_bytes()
            e = row["edit"]
            mutated = original[: e["start"]] + e["replacement"].encode("utf-8") + original[e["end"] :]
            art = from_mutated(path, original, mutated, {})
            row["aid"] = art.aid
            ARTIFACTS[art.aid] = {"path": path, "edit": {"start": art.edit.start, "end": art.edit.end,
                                                         "replacement": art.edit.replacement}}
            continue
        candidates = by_id.get(str(row["native_id"]), [])
        if path:
            candidates = [c for c in candidates if c[0] == path]  # never across a conflicting path
        if len(candidates) == 1:
            row["path"], row["aid"] = candidates[0]
        elif ctx.tool == "poodle" and "poodle" in row:
            row["aid"] = _poodle_aid(row["poodle"], pristine)
    if ctx.tool == "deployproof":
        _map_deployproof(rows, mapping, pristine)
    for row in rows:
        row.pop("poodle", None)
        row.pop("native_diff", None)
        row.pop("dp", None)


def _map_deployproof(rows: list[dict[str, Any]], mapping: dict[tuple[str, str], str], pristine: Path) -> None:
    """Survivors match adapter mutants by file, line and mutated line text first. The survivors of one line and
    description that no text matched then take the adapter mutants of that line and description when there are
    exactly as many: every one of them survived, so which survivor is which does not change a verdict. A survivor
    still unmatched takes the only unclaimed adapter mutant of its line. Every other adapter mutant is a native kill
    only when the report reconciles completely: each reported survivor matched exactly one adapter mutant, the
    report's survivors and kills add up to its total, the total equals the adapter's enumeration, and no runner
    error occurred. Otherwise the kills stay unmapped (no identity can be inferred)."""
    by_line: dict[tuple[str, int], list[tuple[str, str, str]]] = {}
    for (path, native), aid in mapping.items():
        line = int(native.split(":")[1]) if native.count(":") >= 2 else -1
        data = ARTIFACTS[aid]
        text = (pristine / path).read_bytes()
        e = data["edit"]
        mutated = (text[: e["start"]] + e["replacement"].encode("utf-8") + text[e["end"] :]).decode("utf-8")
        mutated_line = " ".join(mutated.splitlines()[line - 1].split()) if 0 < line <= len(mutated.splitlines()) else ""
        by_line.setdefault((path, line), []).append((aid, mutated_line, OPERATORS.get((path, native), "")))
    claimed: set[str] = set()
    survivors = [row for row in rows if row.get("dp")]
    for row in survivors:
        dp = row["dp"]
        exact = [aid for aid, text, _ in by_line.get((row["path"], dp["line"]), [])
                 if aid not in claimed and text == dp["mutated_line"]]
        if exact:
            row["aid"] = exact[0]
            claimed.add(exact[0])
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for row in survivors:
        if not row.get("aid"):
            groups.setdefault((row["path"], row["dp"]["line"], row["dp"]["description"]), []).append(row)
    for (path, line, description), group in groups.items():
        alike = [aid for aid, _, operator in by_line.get((path, line), [])
                 if aid not in claimed and operator == description]
        if len(alike) == len(group):
            for row, aid in zip(group, alike, strict=True):
                row["aid"] = aid
                claimed.add(aid)
    for row in survivors:
        if not row.get("aid"):
            options = [aid for aid, _, _ in by_line.get((row["path"], row["dp"]["line"]), []) if aid not in claimed]
            if len(options) == 1:
                row["aid"] = options[0]
                claimed.add(options[0])
    counts = next((json.loads(r["native_status"]) for r in rows if r["native_id"] == "<counts>"), {})
    reconciled = (all(row.get("aid") for row in survivors)
                  and counts.get("survived_mutants_count") == len(survivors)
                  and counts.get("killed_mutants", -1) + len(survivors) == counts.get("total_mutants")
                  and counts.get("total_mutants") == len(mapping)
                  and not counts.get("runner_errors_count"))
    if reconciled:
        for (path, native), aid in mapping.items():
            if aid not in claimed:
                rows.append({"native_id": native, "native_status": "killed (by count)", "verdict": "killed",
                             "path": path, "aid": aid})


def _poodle_aid(mutant: dict[str, Any], pristine: Path) -> str:
    source = Path(mutant["source_file"])
    rel = os.path.relpath(source, RUN) if source.is_absolute() else mutant["source_file"]
    lines = (pristine / rel).read_text("utf-8").splitlines(keepends=True)
    prefix = lines[mutant["lineno"] - 1][: mutant["col_offset"]]
    suffix = lines[mutant["end_lineno"] - 1][mutant["end_col_offset"] :]
    new = [*lines[: mutant["lineno"] - 1], prefix + mutant["text"] + suffix, *lines[mutant["end_lineno"] :]]
    original = (pristine / rel).read_bytes()
    art = from_mutated(rel, original, "".join(new).encode("utf-8"), {})
    ARTIFACTS[art.aid] = {"path": rel, "edit": {"start": art.edit.start, "end": art.edit.end,
                                                "replacement": art.edit.replacement}}
    return art.aid


class PeakMeter:
    """The container's memory high-water mark (cgroup v2 ``memory.peak``) from the moment the meter starts. A
    kernel that lets a reader reset the mark (per open file, Linux 6.12 and later) measures from then on; an older
    one reports the peak since the container started. ``scope`` says which one applied."""

    def __init__(self) -> None:
        path = Path("/sys/fs/cgroup/memory.peak")
        self.handle = None
        self.scope = "unavailable"
        if not path.exists():
            return
        try:
            self.handle = path.open("r+", encoding="ascii")
            self.handle.write("reset\n")
            self.handle.flush()
            self.scope = "campaign"
        except OSError:
            if self.handle is None:
                self.handle = path.open(encoding="ascii")
            self.scope = "container lifetime (control run included)"

    def read_mib(self) -> float | None:
        if self.handle is None:
            return None
        self.handle.seek(0)
        return round(int(self.handle.read().strip()) / 2**20, 1)


def _tasks_peak() -> int | None:
    """The most tasks (processes and threads) the container ever held (cgroup v2 ``pids.peak``, control run
    included): the parallelism a tool chose, beside the CPUs it could use."""
    path = Path("/sys/fs/cgroup/pids.peak")
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def main() -> None:
    global RUN  # noqa: PLW0603 - the one process-wide project root, fixed before anything runs
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, choices=sorted(CAMPAIGNS))
    parser.add_argument("--files", required=True)
    parser.add_argument("--tests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", default=str(RUN), help="the project the campaign runs in")
    parser.add_argument("--limit", type=int, default=7200, help="seconds one command of the campaign may run")
    args = parser.parse_args()
    RUN = Path(args.root)
    ctx = Ctx(args.tool, VENV.get(args.tool, args.tool), args.files.split(","), args.tests.split(","),
              limit=args.limit)
    pristine = Path("/work/pristine")
    shutil.copytree(RUN, pristine, symlinks=True)
    # Environment parity: the project's own pytest run in the tool's environment, before the campaign.
    control = ctx.sh(str(ctx.bin / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header",
                     "-rN", *ctx.tests)
    control_tail = (control.stdout.strip().splitlines() or [""])[-1]
    shutil.rmtree(RUN, ignore_errors=True)
    shutil.copytree(pristine, RUN, symlinks=True)
    ctx.log.clear()
    meter = PeakMeter()
    # The mark before the campaign: when the meter cannot reset, a final peak above it was reached by the campaign.
    control_peak = meter.read_mib()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    rows = CAMPAIGNS[args.tool](ctx)
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak = meter.read_mib()  # before the mapping below, which is the harness's work, not the tool's
    map_rows(ctx, rows, pristine)
    result = {
        "tool": args.tool, "files": ctx.files, "wall_seconds": round(wall, 2),
        "cpu_seconds": round((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), 2),
        # The largest single process (control run included), not the simultaneous memory of the process tree;
        # the container's peak over the campaign (see cgroup_peak_scope) bounds the latter.
        "largest_process_rss_mib": round(after.ru_maxrss / 1024, 1), "cgroup_peak_mib": peak,
        "cgroup_peak_scope": meter.scope, "cgroup_peak_before_campaign_mib": control_peak,
        "cpus_visible": os.cpu_count(), "cpus_usable": len(os.sched_getaffinity(0)), "tasks_peak": _tasks_peak(),
        "mutants": rows,
        "mapped": sum(1 for r in rows if r.get("aid")), "log": ctx.log[-12:], "stated_error": ctx.stated_error,
        "control_run": {"returncode": control.returncode, "summary": control_tail},
        "artifacts": [{"aid": aid, **data} for aid, data in sorted(ARTIFACTS.items())],
    }
    Path(args.out).write_text(json.dumps(result), encoding="utf-8")
    print(f"{args.tool}: {len(rows)} native mutants, {result['mapped']} mapped, {result['wall_seconds']}s wall")


if __name__ == "__main__":
    main()
