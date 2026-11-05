"""The HTML report: one self-contained page of a campaign for people (a team, a reviewer, a manager).

The page is built from the campaign's own results and the sources it mutated, never from the verdict cache: the cache
also holds rows of earlier versions of the same files, whose lines no longer match the source shown. It needs no
network: the styles, the script, the highlighted sources and the data are all inline.

A small JSON summary is embedded under a fixed id. The next campaign reads it back as its baseline: the new page
compares the two runs and carries the score history forward, so a series of reports draws its own trend without a
store of its own.
"""

from __future__ import annotations

import html
import json
import keyword
import re
import tokenize
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TypedDict, cast

from balamut import __version__
from balamut.config import DEFAULT_SEMANTIC, SemanticPatterns
from balamut.core.semantic import semantic_classes
from balamut.core.source_spans import line_byte_starts
from balamut.core.triage import Bucket, classify, cold_kind
from balamut.engine.results import MutantResult, MutantStatus
from balamut.report.agent import Cluster, MutantRecord, build_report
from balamut.runtime.inmemory import code_reachable

SUMMARY_SCHEMA = "balamut-report-summary/1"
_HISTORY_CAP = 50  # earlier runs a report carries: enough for a long trend, bounded in size
_COVERING_CAP = 12  # covering tests listed per mutant; the total is always shown
_CLUSTERS_CAP = 12  # survivor groups on the summary page
_SUMMARY_ID = "balamut-summary"
_SUMMARY_RE = re.compile(rf'<script type="application/json" id="{_SUMMARY_ID}">(.*?)</script>', re.DOTALL)

_TOKEN_CLASS = {
    tokenize.COMMENT: "c",
    tokenize.NUMBER: "n",
    tokenize.STRING: "s",
    tokenize.FSTRING_START: "s",
    tokenize.FSTRING_MIDDLE: "s",
    tokenize.FSTRING_END: "s",
    tokenize.TSTRING_START: "s",
    tokenize.TSTRING_MIDDLE: "s",
    tokenize.TSTRING_END: "s",
}
_KEYWORDS = frozenset(keyword.kwlist)
_DEFINERS = frozenset({"def", "class"})
_SITES_PER_FILE = 6  # site links per file in a survivor group

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type _Json = dict[str, JsonValue]
type _Classes = list[str]


class VerdictCounts(TypedDict):
    killed: int
    survived: int
    timeout: int
    no_coverage: int


class RunPoint(TypedDict):
    """One earlier run in a report's history."""

    generated_at: str  # ISO 8601 with the UTC offset
    commit: str
    score: float | None
    counts: VerdictCounts


class ReportSummary(TypedDict):
    """The embedded, comparable part of a report: what the next report reads as its baseline."""

    schema: str
    project: str
    generated_at: str
    commit: str
    branch: str
    scope: list[str]
    kinds: list[str]
    score: float | None  # killed among covered mutants; None when no covered mutant ran
    counts: VerdictCounts
    files: VerdictCountsByFile
    history: list[RunPoint]  # the runs before this one, oldest first


type VerdictCountsByFile = dict[str, VerdictCounts]
type SourceByPath = Mapping[str, str]
type IndexByName = dict[str, int]
type DeltasByName = dict[str, str]
type ResultsByPath = dict[str, list[MutantResult]]


@dataclass(frozen=True, slots=True)
class RunInfo:
    """What the report states about the run besides its verdicts."""

    project: str
    generated_at: datetime
    scope: tuple[str, ...]  # the mutated paths, or the diff the campaign was pinned to
    kinds: tuple[str, ...]
    commit: str  # empty outside a git work tree
    branch: str
    duration_s: float


class _MutantJson(TypedDict):
    """One mutant as the file view draws it: positions are 1-based lines and character columns."""

    line: int
    col: int
    end_line: int
    end_col: int
    status: str
    operator: str
    kind: str
    bucket: str  # the survivor's priority; empty for any other verdict
    mutated: str
    semantic: list[str]
    reason: str
    killed_by: list[int]  # indices into the test table
    covering: list[int]
    covering_total: int
    cold: str  # the cold category of a module or class level no-coverage site; empty otherwise


class _FileJson(TypedDict):
    path: str
    lines: list[str]  # highlighted, escaped HTML per source line
    mutants: list[_MutantJson]


class _DataJson(TypedDict):
    files: list[_FileJson]
    tests: list[str]


type ScriptPayload = JsonValue | ReportSummary | _DataJson


@dataclass(frozen=True, slots=True)
class _Breakdown:
    name: str
    counts: VerdictCounts


# --- the summary contract ------------------------------------------------------------------------------------------


def _empty_counts() -> VerdictCounts:
    return {"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0}


def _add(counts: VerdictCounts, status: MutantStatus) -> None:
    match status:
        case MutantStatus.KILLED:
            counts["killed"] += 1
        case MutantStatus.SURVIVED:
            counts["survived"] += 1
        case MutantStatus.TIMEOUT:
            counts["timeout"] += 1
        case MutantStatus.NO_COVERAGE:
            counts["no_coverage"] += 1
        case MutantStatus.UNSUPPORTED:
            pass


def _total(counts: VerdictCounts) -> int:
    return counts["killed"] + counts["survived"] + counts["timeout"] + counts["no_coverage"]


def mutation_score(counts: VerdictCounts) -> float | None:
    """Killed among the covered mutants (killed + survived), as every balamut report counts it; None when none ran."""
    covered = counts["killed"] + counts["survived"]
    return counts["killed"] / covered if covered else None


def _rounded(score: float | None) -> float | None:
    return None if score is None else round(score, 4)


def _judged(results: Iterable[MutantResult]) -> list[MutantResult]:
    """Results with a verdict: an unsupported mutant never ran and is not part of any count."""
    return [result for result in results if result.status is not MutantStatus.UNSUPPORTED]


def _point(summary: ReportSummary) -> RunPoint:
    return {
        "generated_at": summary["generated_at"],
        "commit": summary["commit"],
        "score": summary["score"],
        "counts": summary["counts"],
    }


def build_summary(results: list[MutantResult], info: RunInfo, baseline: ReportSummary | None = None) -> ReportSummary:
    """Totals and per-file counts of the run, with the baseline's history extended by the baseline itself."""
    counts = _empty_counts()
    files: VerdictCountsByFile = {}
    for result in _judged(results):
        _add(counts, result.status)
        _add(files.setdefault(result.mutant.path, _empty_counts()), result.status)
    history = [] if baseline is None else [*baseline["history"], _point(baseline)][-_HISTORY_CAP:]
    return {
        "schema": SUMMARY_SCHEMA,
        "project": info.project,
        "generated_at": info.generated_at.isoformat(timespec="seconds"),
        "commit": info.commit,
        "branch": info.branch,
        "scope": list(info.scope),
        "kinds": list(info.kinds),
        "score": _rounded(mutation_score(counts)),
        "counts": counts,
        "files": dict(sorted(files.items())),
        "history": history,
    }


def _object(value: JsonValue, what: str) -> _Json:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    return cast("_Json", value)


def _string(obj: _Json, key: str, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{what}.{key} must be a string")
    return value


def _timestamp(obj: _Json, key: str, what: str) -> str:
    value = _string(obj, key, what)
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{what}.{key} must be an ISO 8601 timestamp") from exc
    return value


def _strings(obj: _Json, key: str, what: str) -> list[str]:
    value = obj.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in cast("list[object]", value)):
        raise ValueError(f"{what}.{key} must be a list of strings")
    return cast("list[str]", value)


def _count(obj: _Json, key: str, what: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what}.{key} must be a non-negative integer")
    return value


def _score_field(obj: _Json, key: str, what: str) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError(f"{what}.{key} must be null or a number between 0 and 1")
    return float(value)


def _counts_field(value: JsonValue, what: str) -> VerdictCounts:
    obj = _object(value, what)
    return {
        "killed": _count(obj, "killed", what),
        "survived": _count(obj, "survived", what),
        "timeout": _count(obj, "timeout", what),
        "no_coverage": _count(obj, "no_coverage", what),
    }


def _history_field(value: JsonValue) -> list[RunPoint]:
    if not isinstance(value, list):
        raise ValueError("summary.history must be a list")
    points: list[RunPoint] = []
    for index, item in enumerate(cast("list[JsonValue]", value)):
        what = f"summary.history[{index}]"
        obj = _object(item, what)
        points.append(
            {
                "generated_at": _timestamp(obj, "generated_at", what),
                "commit": _string(obj, "commit", what),
                "score": _score_field(obj, "score", what),
                "counts": _counts_field(obj.get("counts"), f"{what}.counts"),
            }
        )
    return points


def parse_summary(raw: JsonValue) -> ReportSummary:
    """Validate a decoded summary: an unknown schema or a malformed field is a ValueError."""
    obj = _object(raw, "summary")
    schema = obj.get("schema")
    if schema != SUMMARY_SCHEMA:
        raise ValueError(f"unsupported summary schema {schema!r}; expected {SUMMARY_SCHEMA!r}")
    files = _object(obj.get("files"), "summary.files")
    return {
        "schema": SUMMARY_SCHEMA,
        "project": _string(obj, "project", "summary"),
        "generated_at": _timestamp(obj, "generated_at", "summary"),
        "commit": _string(obj, "commit", "summary"),
        "branch": _string(obj, "branch", "summary"),
        "scope": _strings(obj, "scope", "summary"),
        "kinds": _strings(obj, "kinds", "summary"),
        "score": _score_field(obj, "score", "summary"),
        "counts": _counts_field(obj.get("counts"), "summary.counts"),
        "files": {path: _counts_field(counts, f"summary.files[{path!r}]") for path, counts in files.items()},
        "history": _history_field(obj.get("history")),
    }


def read_baseline(path: Path) -> ReportSummary:
    """The summary embedded in an earlier HTML report."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    found = _SUMMARY_RE.search(text)
    if found is None:
        raise ValueError(f"{path} is not a balamut HTML report: it embeds no summary")
    try:
        raw: JsonValue = json.loads(found.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"the summary embedded in {path} is not valid JSON: {exc}") from exc
    return parse_summary(raw)


# --- sources -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Source:
    """A module's text addressed the way mutants are: lines as the tokenizer counts them, spans in UTF-8 bytes."""

    data: bytes
    starts: list[int]  # byte offset of each line start
    lines: list[str]  # line text without its line break

    @classmethod
    def of(cls, source: str) -> _Source:
        data = source.encode("utf-8")
        starts = line_byte_starts(source)
        if len(starts) > 1 and starts[-1] == len(data):  # a final line break opens no line of its own
            starts = starts[:-1]
        ends = [*starts[1:], len(data)]
        lines = [data[start:end].decode("utf-8").rstrip("\r\n") for start, end in zip(starts, ends, strict=True)]
        return cls(data, starts, lines)

    def position(self, offset: int) -> tuple[int, int]:
        """(1-based line, character column) of a byte offset."""
        index = bisect_right(self.starts, offset) - 1
        return index + 1, len(self.data[self.starts[index] : offset].decode("utf-8"))

    def span(self, start: int, end: int) -> tuple[int, int, int, int]:
        """(line, col, end line, end col) of a byte span; an end at a line start closes the line before it."""
        line, col = self.position(start)
        end_line, end_col = self.position(end)
        if end_line > line and end_col == 0:
            end_line -= 1
            end_col = len(self.lines[end_line - 1])
        return line, col, end_line, end_col


def _mark(classes: list[_Classes], token: tokenize.TokenInfo, css: str) -> None:
    (start_row, start_col), (end_row, end_col) = token.start, token.end
    for row in range(start_row, min(end_row, len(classes)) + 1):
        line = classes[row - 1]
        first = start_col if row == start_row else 0
        last = end_col if row == end_row else len(line)
        for column in range(first, min(last, len(line))):
            line[column] = css


def _render_line(text: str, classes: _Classes) -> str:
    parts: list[str] = []
    index = 0
    while index < len(text):
        css = classes[index]
        stop = index + 1
        while stop < len(text) and classes[stop] == css:
            stop += 1
        chunk = html.escape(text[index:stop], quote=False)
        parts.append(f'<span class="{css}">{chunk}</span>' if css else chunk)
        index = stop
    return "".join(parts)


def highlight_lines(lines: list[str]) -> list[str]:
    """Escaped HTML per source line: keywords, defined names, strings, numbers and comments get a class."""
    classes: list[_Classes] = [[""] * len(line) for line in lines]
    feed = iter([f"{line}\n" for line in lines])
    previous = ""
    for token in tokenize.generate_tokens(lambda: next(feed, "")):
        css = _TOKEN_CLASS.get(token.type, "")
        if token.type == tokenize.NAME:
            if token.string in _KEYWORDS:
                css = "k"
            elif previous in _DEFINERS:
                css = "d"
        if css and token.start[0] <= len(lines):
            _mark(classes, token, css)
        if token.type in {tokenize.NAME, tokenize.OP}:
            previous = token.string
    return [_render_line(line, line_classes) for line, line_classes in zip(lines, classes, strict=True)]


# --- the model -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Located:
    result: MutantResult
    line: int
    col: int
    end_line: int
    end_col: int
    cold: str


def _locate(result: MutantResult, text: _Source, source: str) -> _Located:
    m = result.mutant
    line, col, end_line, end_col = text.span(m.start_byte, m.end_byte)
    cold = ""
    if result.status is MutantStatus.NO_COVERAGE and not code_reachable(source, m.lineno):
        cold = cold_kind(source, line=m.site_lineno, start_byte=m.start_byte, end_byte=m.end_byte).value
    return _Located(result, line, col, end_line, end_col, cold)


class _Tests:
    """The test table: every node id once, referenced by index from the mutants."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._index: IndexByName = {}

    def refs(self, nodeids: Iterable[str]) -> list[int]:
        refs: list[int] = []
        for nodeid in nodeids:
            if nodeid not in self._index:
                self._index[nodeid] = len(self.names)
                self.names.append(nodeid)
            refs.append(self._index[nodeid])
        return refs


def _mutant_json(located: _Located, semantic: list[str], tests: _Tests) -> _MutantJson:
    result = located.result
    m = result.mutant
    covering = result.covering_tests
    return {
        "line": located.line,
        "col": located.col,
        "end_line": located.end_line,
        "end_col": located.end_col,
        "status": result.status.value,
        "operator": m.operator,
        "kind": m.family,
        "bucket": classify(m).value if result.status is MutantStatus.SURVIVED else "",
        "mutated": m.mutated,
        "semantic": semantic,
        "reason": result.reason.value,
        "killed_by": tests.refs(result.killed_by),
        "covering": tests.refs(covering[:_COVERING_CAP]),
        "covering_total": len(covering),
        "cold": located.cold,
    }


def _record(located: _Located, semantic: list[str]) -> MutantRecord:
    """The agent report's row for one mutant, on the line the page shows it (the line of its span)."""
    result = located.result
    m = result.mutant
    return {
        "key": result.key,
        "file": m.path,
        "lineno": located.line,
        "operator": m.operator,
        "verdict": result.status.value,
        "bucket": classify(m).value,
        "original": m.original,
        "mutated": m.mutated,
        "covering": list(result.covering_tests),
        "semantic": semantic,
        "kind": m.family,
    }


def _breakdown(rows: Iterable[tuple[str, MutantStatus]]) -> list[_Breakdown]:
    grouped: VerdictCountsByFile = {}
    for name, status in rows:
        _add(grouped.setdefault(name, _empty_counts()), status)
    ordered = sorted(grouped.items(), key=lambda item: (-_total(item[1]), item[0]))
    return [_Breakdown(name, counts) for name, counts in ordered]


@dataclass(frozen=True, slots=True)
class _Model:
    summary: ReportSummary
    data: _DataJson
    file_ids: IndexByName
    by_kind: list[_Breakdown]
    by_semantic: list[_Breakdown]
    clusters: list[Cluster]
    cluster_total: int
    high: int
    cold: int
    unsupported: int


def _model(
    results: list[MutantResult],
    sources: SourceByPath,
    *,
    info: RunInfo,
    semantic: SemanticPatterns,
    baseline: ReportSummary | None,
) -> _Model:
    judged = _judged(results)
    by_path: ResultsByPath = {}
    for result in judged:
        by_path.setdefault(result.mutant.path, []).append(result)
    tests = _Tests()
    files: list[_FileJson] = []
    records: list[MutantRecord] = []
    kind_rows: list[tuple[str, MutantStatus]] = []
    semantic_rows: list[tuple[str, MutantStatus]] = []
    cold = 0
    for path in sorted(by_path):
        source = sources[path]
        text = _Source.of(source)
        located = sorted(
            (_locate(result, text, source) for result in by_path[path]),
            key=lambda item: (item.line, item.col, item.result.mutant.operator),
        )
        mutants: list[_MutantJson] = []
        for item in located:
            classes = semantic_classes(item.result.mutant, semantic)
            mutants.append(_mutant_json(item, classes, tests))
            if not item.cold:
                records.append(_record(item, classes))
            cold += bool(item.cold)
            kind_rows.append((item.result.mutant.family or "unlabeled", item.result.status))
            semantic_rows.extend((name, item.result.status) for name in classes)
        files.append({"path": path, "lines": highlight_lines(text.lines), "mutants": mutants})
    report = build_report(records)
    high = sum(
        1 for result in judged if result.status is MutantStatus.SURVIVED and classify(result.mutant) is Bucket.HIGH
    )
    return _Model(
        summary=build_summary(results, info, baseline),
        data={"files": files, "tests": tests.names},
        file_ids={file["path"]: index for index, file in enumerate(files)},
        by_kind=_breakdown(kind_rows),
        by_semantic=_breakdown(semantic_rows),
        clusters=report["actionable_clusters"][:_CLUSTERS_CAP],
        cluster_total=len(report["actionable_clusters"]),
        high=high,
        cold=cold,
        unsupported=len(results) - len(judged),
    )


# --- rendering -----------------------------------------------------------------------------------------------------


def _e(text: JsonValue) -> str:
    return html.escape(str(text))


def _int(value: int) -> str:
    return f"{value:,}"


def _pct(score: float | None) -> str:
    return "&mdash;" if score is None else f"{score * 100:.1f}%"


def _duration(seconds: float) -> str:
    total = round(seconds)
    if total < 1:
        return "under 1 s"
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _date(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M")


def _script_json(value: ScriptPayload) -> str:
    """JSON safe inside a ``<script>`` element: no ``<`` can close it early."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _asset(name: str) -> str:
    return resources.files("balamut.report").joinpath("assets", name).read_text(encoding="utf-8")


def _segments(counts: VerdictCounts, *, label: str) -> str:
    """A thin bar of one whole split by verdict; every part names itself on hover and focus."""
    total = _total(counts)
    parts = (
        ("killed", "Killed", counts["killed"]),
        ("survived", "Survived", counts["survived"]),
        ("timeout", "Timeout", counts["timeout"]),
        ("no_coverage", "No coverage", counts["no_coverage"]),
    )
    spans = "".join(
        f'<span class="seg-part v-{css}" style="flex-grow:{value}" data-tip="{_e(name)}: {_int(value)} of '
        f'{_int(total)} ({value / total:.0%})"></span>'
        for css, name, value in parts
        if value
    )
    aria = ", ".join(f"{name} {value}" for _css, name, value in parts)
    return f'<span class="seg" role="img" aria-label="{_e(label)}: {_e(aria)}">{spans}</span>'


def _delta(now: float, then: float, *, unit: str, higher_is_better: bool, scale: float = 1.0) -> str:
    diff = (now - then) * scale
    if abs(diff) < 0.05:  # below the shown precision
        return '<span class="delta flat">no change</span>'
    better = (diff > 0) == higher_is_better
    arrow = "&#9650;" if diff > 0 else "&#9660;"
    shown = f"{abs(diff):.1f}" if unit == "pp" else _int(round(abs(diff)))
    sign = "+" if diff > 0 else "&minus;"
    words = "better" if better else "worse"
    return f'<span class="delta {"up" if better else "down"}">{arrow} {sign}{shown}{unit} <em>{words}</em></span>'


def _masthead(info: RunInfo, summary: ReportSummary) -> str:
    where = " &middot; ".join(
        part
        for part in (
            f"<code>{_e(info.branch)}</code>" if info.branch else "",
            f"<code>{_e(info.commit)}</code>" if info.commit else "",
        )
        if part
    )
    meta = [
        ("Generated", _e(_date(summary["generated_at"]))),
        ("Revision", where or "outside git"),
        ("Scope", " ".join(f"<code>{_e(path)}</code>" for path in info.scope)),
        ("Kinds", " ".join(f"<code>{_e(kind)}</code>" for kind in info.kinds)),
        ("Run time", _e(_duration(info.duration_s))),
    ]
    items = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in meta)
    return (
        '<header class="masthead"><div class="wrap masthead-inner">'
        '<div class="brand"><span class="wordmark">balamut</span><span class="brand-sub">mutation report</span></div>'
        f'<h1 class="project">{_e(info.project)}</h1>'
        f'<dl class="meta">{items}</dl>'
        "</div></header>"
    )


def _tile(label: str, value: int, css: str, note: str, delta: str = "") -> str:
    return (
        f'<div class="tile"><div class="tile-label"><span class="dot v-{css}" aria-hidden="true"></span>{label}</div>'
        f'<div class="tile-value">{_int(value)}</div>'
        f'<div class="tile-note">{note}</div>{delta}</div>'
    )


def _hero(model: _Model, baseline: ReportSummary | None) -> str:
    summary = model.summary
    counts = summary["counts"]
    total = _total(counts)
    score_delta = ""
    deltas: DeltasByName = {}
    if baseline is not None:
        then = baseline["counts"]
        if summary["score"] is not None and baseline["score"] is not None:
            score_delta = _delta(summary["score"], baseline["score"], unit="pp", higher_is_better=True, scale=100)
        deltas = {
            "killed": _delta(counts["killed"], then["killed"], unit="", higher_is_better=True),
            "survived": _delta(counts["survived"], then["survived"], unit="", higher_is_better=False),
            "no_coverage": _delta(counts["no_coverage"], then["no_coverage"], unit="", higher_is_better=False),
            "timeout": _delta(counts["timeout"], then["timeout"], unit="", higher_is_better=False),
        }
    files = len(model.data["files"])
    reached = total - counts["no_coverage"]
    lede = (
        f"balamut planted <strong>{_int(total)}</strong> small faults in <strong>{_int(files)}</strong> "
        f"{'file' if files == 1 else 'files'}. The tests reached <strong>{_int(reached)}</strong> of them "
        f"({_pct(reached / total if total else None)}) and caught <strong>{_int(counts['killed'])}</strong>."
    )
    since = f'<span class="since">since {_e(_date(baseline["generated_at"]))}</span>' if baseline else ""
    tiles = "".join(
        (
            _tile("Killed", counts["killed"], "killed", "a test failed on them", deltas.get("killed", "")),
            _tile(
                "Survived",
                counts["survived"],
                "survived",
                f"{_int(model.high)} high priority",
                deltas.get("survived", ""),
            ),
            _tile(
                "No coverage",
                counts["no_coverage"],
                "no_coverage",
                f"{_int(model.cold)} at import time" if model.cold else "no test runs this code",
                deltas.get("no_coverage", ""),
            ),
            _tile("Timeout", counts["timeout"], "timeout", "unresolved hangs", deltas.get("timeout", "")),
        )
    )
    return (
        '<section class="hero wrap" aria-labelledby="score-label">'
        '<div class="score">'
        '<div class="eyebrow" id="score-label">Mutation score</div>'
        f'<div class="score-value">{_pct(summary["score"])}</div>'
        f'<div class="score-delta">{score_delta}{since}</div>'
        f'<p class="lede">{lede}</p>'
        '<p class="formula">killed &divide; (killed + survived): the share of the faults a test executed that the '
        "suite caught</p>"
        "</div>"
        f'<div class="tiles">{tiles}</div>'
        '<figure class="mosaic-card">'
        '<figcaption><span class="card-title">Every mutant, file by file</span>'
        '<span class="legend">'
        f'<span><i class="sw v-killed"></i>Killed {_int(counts["killed"])}</span>'
        f'<span><i class="sw v-survived"></i>Survived {_int(counts["survived"])}</span>'
        f'<span><i class="sw v-no_coverage"></i>No coverage {_int(counts["no_coverage"])}</span>'
        f'<span><i class="sw v-timeout"></i>Timeout {_int(counts["timeout"])}</span>'
        "</span></figcaption>"
        f'<canvas id="mosaic" role="img" aria-label="{_int(total)} mutants by verdict, in file and line order">'
        "</canvas>"
        '<p class="hint">Each square is one planted fault, in file and line order. '
        "Hover to inspect it, click to open its code.</p>"
        "</figure>"
        "</section>"
    )


def _trend(points: list[RunPoint]) -> str:
    """The score of every run, oldest first, on one axis from a clean floor to 100%."""
    scored = [(point, point["score"]) for point in points if point["score"] is not None]
    if len(scored) < 2:  # a line needs two runs
        return ""
    values = [score * 100 for _point, score in scored]
    floor = max(0, min(int(min(values) // 10 * 10), 90))
    width, height, left, right, top, bottom = 640, 220, 44, 24, 16, 36
    plot_w, plot_h = width - left - right, height - top - bottom

    def x(index: int) -> float:
        return left + plot_w * index / (len(scored) - 1)

    def y(value: float) -> float:
        return top + plot_h * (100 - value) / (100 - floor)

    ticks = range(floor, 101, 10 if 100 - floor > 20 else 5)  # tick spacing by range
    grid = "".join(
        f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"></line>'
        f'<text class="axis" x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{t}%</text>'
        for t in ticks
    )
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{left},{top + plot_h} {line} {x(len(values) - 1):.1f},{top + plot_h}"
    step = max(1, len(scored) // 6)
    same_day = len({datetime.fromisoformat(point["generated_at"]).date() for point, _score in scored}) == 1
    axis_format = "%H:%M" if same_day else "%d %b"
    labels = "".join(
        f'<text class="axis" x="{x(i):.1f}" y="{height - 12}" text-anchor="middle">'
        f"{_e(datetime.fromisoformat(point['generated_at']).strftime(axis_format))}</text>"
        for i, (point, _score) in enumerate(scored)
        if i % step == 0 or i == len(scored) - 1
    )
    dots = "".join(
        f'<circle class="pt{" last" if i == len(values) - 1 else ""}" cx="{x(i):.1f}" cy="{y(v):.1f}" '
        f'r="{6 if i == len(values) - 1 else 4}" tabindex="0" '
        f'data-tip="{_e(_date(point["generated_at"]))}: {v:.1f}%'
        f'{" at " + _e(point["commit"]) if point["commit"] else ""}"></circle>'
        for i, ((point, _score), v) in enumerate(zip(scored, values, strict=True))
    )
    last = values[-1]
    end_label = (
        f'<text class="end-label" x="{x(len(values) - 1) - 10:.1f}" y="{y(last) - 12:.1f}" '
        f'text-anchor="end">{last:.1f}%</text>'
    )
    rows = "".join(
        f"<tr><td>{_e(_date(point['generated_at']))}</td><td><code>{_e(point['commit'] or '-')}</code></td>"
        f"<td class='num'>{v:.1f}%</td><td class='num'>{_int(point['counts']['survived'])}</td></tr>"
        for (point, _score), v in zip(scored, values, strict=True)
    )
    return (
        '<figure class="trend">'
        '<figcaption class="card-title">Mutation score by run</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Mutation score over {len(values)} runs, '
        f'from {values[0]:.1f}% to {last:.1f}%">'
        f'{grid}<polygon class="area" points="{area}"></polygon><polyline class="line" points="{line}"></polyline>'
        f"{dots}{end_label}{labels}</svg>"
        '<details class="table-view"><summary>Show as a table</summary>'
        '<div class="scroll"><table><thead><tr><th>Run</th><th>Commit</th><th class="num">Score</th>'
        f'<th class="num">Survived</th></tr></thead><tbody>{rows}</tbody></table></div></details>'
        "</figure>"
    )


def _progress(summary: ReportSummary, baseline: ReportSummary | None) -> str:
    if baseline is None:
        return ""
    then, now = baseline["counts"], summary["counts"]
    score_change = ""
    if summary["score"] is not None and baseline["score"] is not None:
        score_change = _delta(summary["score"], baseline["score"], unit="pp", higher_is_better=True, scale=100)

    def fewer_is_better(label: str, before: int, after: int) -> tuple[str, str, str, str]:
        return label, _int(before), _int(after), _delta(after, before, unit="", higher_is_better=False)

    rows = [
        ("Mutation score", _pct(baseline["score"]), _pct(summary["score"]), score_change),
        fewer_is_better("Survived", then["survived"], now["survived"]),
        fewer_is_better("No coverage", then["no_coverage"], now["no_coverage"]),
        fewer_is_better("Timeout", then["timeout"], now["timeout"]),
        ("Mutants", _int(_total(then)), _int(_total(now)), ""),
    ]
    body = "".join(
        f'<tr><th scope="row">{label}</th><td class="num">{before}</td><td class="num">{after}</td>'
        f"<td>{delta}</td></tr>"
        for label, before, after, delta in rows
    )
    note = ""
    if baseline["scope"] != summary["scope"] or baseline["kinds"] != summary["kinds"]:
        note = (
            '<p class="note">The baseline run mutated another scope '
            f"({_e(' '.join(baseline['scope']))}; kinds {_e(', '.join(baseline['kinds']))}): "
            "the counts compare different sets of mutants.</p>"
        )
    trend = _trend([*summary["history"], _point(summary)])
    return (
        '<section class="wrap section" aria-labelledby="progress-title">'
        '<div class="section-head"><h2 id="progress-title">Progress</h2>'
        f"<p>Compared with the run of {_e(_date(baseline['generated_at']))}"
        f"{' at <code>' + _e(baseline['commit']) + '</code>' if baseline['commit'] else ''}.</p></div>"
        f'<div class="progress-grid{" single" if not trend else ""}">'
        '<div class="compare"><div class="scroll"><table><thead><tr><th></th><th class="num">Then</th>'
        f'<th class="num">Now</th><th>Change</th></tr></thead><tbody>{body}</tbody></table></div>{note}</div>'
        f"{trend}</div></section>"
    )


def _breakdown_rows(rows: list[_Breakdown]) -> str:
    return "".join(
        f'<li><span class="bd-name"><code>{_e(row.name)}</code></span>'
        f"{_segments(row.counts, label=row.name)}"
        f'<span class="bd-score">{_pct(mutation_score(row.counts))}</span>'
        f'<span class="bd-n">{_int(_total(row.counts))}</span></li>'
        for row in rows
    )


def _breakdowns(model: _Model) -> str:
    semantic = ""
    if model.by_semantic:
        semantic = (
            '<div class="panel-block"><h3>By risk area</h3>'
            "<p>Mutants in code that matches the project's risk patterns (tenants, auth, time, transactions, "
            "pagination by default). A survivor here costs more.</p>"
            f'<ul class="bd">{_breakdown_rows(model.by_semantic)}</ul></div>'
        )
    return (
        '<section class="wrap section" aria-labelledby="areas-title">'
        '<div class="section-head"><h2 id="areas-title">Where the tests are strong</h2>'
        "<p>Score and verdict split per group. Score is killed among covered mutants.</p></div>"
        f'<div class="breakdowns{" single" if not semantic else ""}">'
        '<div class="panel-block"><h3>By mutant kind</h3>'
        "<p>Each kind plants a different sort of fault: changed operators and constants, deleted statements, "
        "failing dependencies, weakened assertions.</p>"
        f'<ul class="bd">{_breakdown_rows(model.by_kind)}</ul></div>'
        f"{semantic}</div></section>"
    )


def _sample(sample: str) -> str:
    original, _arrow, mutated = sample.partition(" → ")
    added = f'<span class="smp-add">{_e(mutated[:120])}</span>' if mutated else '<span class="smp-gone">removed</span>'
    return (
        f'<code class="smp"><span class="smp-del">{_e(original[:120])}</span>'
        f'<span class="smp-arrow" aria-label="becomes">&rarr;</span>{added}</code>'
    )


def _fix_next(model: _Model) -> str:
    if not model.clusters:
        return (
            '<section class="wrap section" aria-labelledby="fix-title">'
            '<div class="section-head"><h2 id="fix-title">Fix next</h2></div>'
            '<p class="empty">No high-priority survivors. Every significant fault a test reached was caught.</p>'
            "</section>"
        )
    items: list[str] = []
    for cluster in model.clusters:
        links: list[str] = []
        for site in cluster["sites"]:
            file_id = model.file_ids[site["file"]]
            links.extend(
                f'<a class="site" href="#f{file_id}-l{line}"><code>{_e(site["file"])}:{line}</code></a>'
                for line in site["lines"][:_SITES_PER_FILE]
            )
            if len(site["lines"]) > _SITES_PER_FILE:
                links.append(f'<span class="more">+{len(site["lines"]) - _SITES_PER_FILE} more</span>')
        tests = cluster["covering_test_files"]
        strengthen = (
            "Strengthen " + ", ".join(f"<code>{_e(test)}</code>" for test in tests)
            if tests
            else "No test covers these lines yet."
        )
        semantic = "".join(f'<span class="tag sem">{_e(tag)}</span>' for tag in cluster["semantic"])
        items.append(
            f'<li class="cluster"><div class="cl-count">{_int(cluster["count"])}<span>&times;</span></div>'
            f'<div class="cl-body"><div class="cl-head">{_sample(cluster["sample"])}'
            f'<span class="tag">{_e(cluster["operator_family"])}</span>{semantic}</div>'
            f'<div class="cl-sites">{"".join(links)}</div><p class="cl-tests">{strengthen}</p></div></li>'
        )
    more = ""
    if model.cluster_total > len(model.clusters):
        more = f'<p class="note">{_int(model.cluster_total - len(model.clusters))} smaller groups are in the files.</p>'
    return (
        '<section class="wrap section" aria-labelledby="fix-title">'
        '<div class="section-head"><h2 id="fix-title">Fix next</h2>'
        f"<p>{_int(model.high)} high-priority survivors in {_int(model.cluster_total)} groups. A group is one idiom, "
        "so a single sharper test often kills the whole group.</p></div>"
        f'<ol class="clusters">{"".join(items)}</ol>{more}</section>'
    )


def _files_table(model: _Model, baseline: ReportSummary | None) -> str:
    previous = baseline["files"] if baseline is not None else {}
    rows: list[str] = []
    for index, file in enumerate(model.data["files"]):
        path = file["path"]
        counts = model.summary["files"][path]
        score = mutation_score(counts)
        directory, _slash, name = path.rpartition("/")
        delta_cell = ""
        delta_value = ""
        if baseline is not None:
            before = mutation_score(previous[path]) if path in previous else None
            if score is not None and before is not None:
                delta_value = f"{(score - before) * 100:.4f}"
                delta_cell = _delta(score, before, unit="pp", higher_is_better=True, scale=100)
            else:
                delta_cell = '<span class="delta flat">new</span>' if path not in previous else ""
            delta_cell = f"<td>{delta_cell}</td>"
        rows.append(
            f'<tr data-path="{_e(path)}" data-total="{_total(counts)}" '
            f'data-score="{"" if score is None else f"{score:.6f}"}" data-killed="{counts["killed"]}" '
            f'data-survived="{counts["survived"]}" data-no_coverage="{counts["no_coverage"]}" '
            f'data-timeout="{counts["timeout"]}" data-delta="{delta_value}">'
            f'<td class="path"><a href="#f{index}"><span class="dir">{_e(directory + "/" if directory else "")}</span>'
            f"<span class='name'>{_e(name)}</span></a></td>"
            f"<td>{_segments(counts, label=path)}</td>"
            f'<td class="num strong">{_pct(score)}</td>'
            f'<td class="num">{_int(_total(counts))}</td>'
            f'<td class="num">{_int(counts["killed"])}</td>'
            f'<td class="num{" hot" if counts["survived"] else ""}">{_int(counts["survived"])}</td>'
            f'<td class="num">{_int(counts["no_coverage"])}</td>'
            f'<td class="num">{_int(counts["timeout"])}</td>'
            f"{delta_cell}</tr>"
        )
    columns = [
        ("path", "File", ""),
        ("", "Verdicts", "bar"),
        ("score", "Score", "num"),
        ("total", "Mutants", "num"),
        ("killed", "Killed", "num"),
        ("survived", "Survived", "num"),
        ("no_coverage", "No cov.", "num"),
        ("timeout", "Timeout", "num"),
    ]
    if baseline is not None:
        columns.append(("delta", "Change", ""))
    head = "".join(
        f'<th class="{css}" scope="col" aria-sort="none"><button type="button" data-sort="{key}">{label}</button></th>'
        if key
        else f'<th class="{css}" scope="col">{label}</th>'
        for key, label, css in columns
    )
    return (
        '<section class="wrap section" aria-labelledby="files-title">'
        '<div class="section-head row"><div><h2 id="files-title">Files</h2>'
        "<p>Sorted by survivors. Open a file to read its code with every mutant in place.</p></div>"
        '<label class="search"><span class="visually-hidden">Filter files</span>'
        '<input id="file-filter" type="search" placeholder="Filter by path" autocomplete="off"></label></div>'
        f'<div class="scroll"><table class="files" id="files"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def _glossary(model: _Model) -> str:
    unsupported = ""
    if model.unsupported:
        unsupported = (
            f"<p class='note'>{_int(model.unsupported)} more mutants could not be applied to the running code and are "
            "left out of every count.</p>"
        )
    return (
        '<section class="wrap section glossary" aria-labelledby="glossary-title">'
        '<div class="section-head"><h2 id="glossary-title">How to read this report</h2></div><dl>'
        '<div><dt><span class="dot v-killed"></span>Killed</dt><dd>A test failed on the planted fault and passed '
        "again on the original code. The suite guards this behaviour.</dd></div>"
        '<div><dt><span class="dot v-survived"></span>Survived</dt><dd>Every test that runs the line still passed. '
        "The code is executed but its result is not checked closely enough.</dd></div>"
        '<div><dt><span class="dot v-no_coverage"></span>No coverage</dt><dd>No test runs this line. Import-time '
        "sites (module and class level) are counted apart.</dd></div>"
        '<div><dt><span class="dot v-timeout"></span>Timeout</dt><dd>A test hung and balamut could not prove the '
        "fault caused it. These need another look.</dd></div>"
        "<div><dt>Priority</dt><dd>Survivors are ranked <b>high</b> (decisions, arithmetic, state, returns), "
        "<b>review</b> (constants, unary and augmented operators) or <b>low</b> (likely noise such as logging).</dd>"
        f"</div></dl>{unsupported}</section>"
    )


def _footer(summary: ReportSummary, baseline: ReportSummary | None) -> str:
    hint = (
        "Pass this file to the next run with <code>--html-baseline</code> to compare the runs and draw the trend."
        if baseline is None
        else f"This report carries the history of {_int(len(summary['history']))} earlier "
        f"{'run' if len(summary['history']) == 1 else 'runs'}."
    )
    return (
        f'<footer class="footer"><div class="wrap"><p>Generated by balamut {_e(__version__)} on '
        f"{_e(_date(summary['generated_at']))}. {hint}</p></div></footer>"
    )


def render_html(
    results: list[MutantResult],
    sources: SourceByPath,
    *,
    info: RunInfo,
    semantic: SemanticPatterns = DEFAULT_SEMANTIC,
    baseline: ReportSummary | None = None,
) -> str:
    """The whole page. ``sources`` holds the text of every mutated file as the campaign enumerated it."""
    model = _model(results, sources, info=info, semantic=semantic, baseline=baseline)
    summary = model.summary
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{_e(info.project)} mutation report</title>\n"
        f'<meta name="generator" content="balamut {_e(__version__)}">\n'
        f"<style>\n{_asset('report.css')}</style>\n</head>\n<body>\n"
        f"{_masthead(info, summary)}"
        '<main id="summary">'
        f"{_hero(model, baseline)}{_progress(summary, baseline)}{_breakdowns(model)}{_fix_next(model)}"
        f"{_files_table(model, baseline)}{_glossary(model)}"
        "</main>"
        '<section id="file-view" class="file-view" hidden aria-live="polite"></section>'
        f"{_footer(summary, baseline)}"
        '<div class="tooltip" id="tooltip" role="tooltip" hidden></div>\n'
        f'<script type="application/json" id="{_SUMMARY_ID}">{_script_json(summary)}</script>\n'
        f'<script type="application/json" id="balamut-data">{_script_json(model.data)}</script>\n'
        f"<script>\n{_asset('report.js')}</script>\n</body>\n</html>\n"
    )


def write_html(path: Path, page: str) -> None:
    """Write the page whole: a reader never sees a half-written report, and a failed write leaves no partial file."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(page, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
