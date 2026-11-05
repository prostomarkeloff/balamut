"""The HTML report (balamut.report.html): the summary contract, the highlighted source and the page."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from balamut.core.mutants import Mutant
from balamut.engine.results import MutantResult, MutantStatus, VerdictReason
from balamut.operators.src import enumerate_mutants
from balamut.report import html
from balamut.report.html import (
    ReportSummary,
    RunInfo,
    RunPoint,
    _delta,
    _duration,
    _pct,
    _script_json,
    _Source,
    _trend,
    build_summary,
    highlight_lines,
    mutation_score,
    parse_summary,
    read_baseline,
    render_html,
    write_html,
)

SOURCE = """\
def is_adult(age: int) -> bool:
    # the legal age
    return age >= 18
"""
PATH = "src/pkg/calc.py"
INFO = RunInfo(
    project="pkg",
    generated_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
    scope=("src",),
    kinds=("src",),
    commit="abc123def456",
    branch="main",
    duration_s=42.0,
)

type Json = dict[str, html.JsonValue]


def _mutants(source: str = SOURCE, path: str = PATH) -> list[Mutant]:
    return enumerate_mutants(path, source)


def _result(
    mutant: Mutant,
    status: MutantStatus,
    *,
    covering_tests: tuple[str, ...] = ("tests/t.py::test_a",),
    killed_by: tuple[str, ...] = (),
    reason: VerdictReason = VerdictReason.NONE,
) -> MutantResult:
    key = f"key-{mutant.start_byte}-{mutant.operator}"
    return MutantResult(mutant, status, len(covering_tests), key, covering_tests, killed_by, reason)


def _data(page: str) -> Json:
    found = re.search(r'<script type="application/json" id="balamut-data">(.*?)</script>', page, re.DOTALL)
    assert found is not None
    data: Json = json.loads(found.group(1))
    return data


def _file_mutants(page: str) -> list[Json]:
    files = cast("list[Json]", _data(page)["files"])
    return cast("list[Json]", files[0]["mutants"])


# --- highlighting ---


def test_highlight_classes_tokens_and_escapes_the_text() -> None:
    lines = highlight_lines(['if s == "<b>":  # a & b', "    x = 10"])
    assert lines[0] == (
        '<span class="k">if</span> s == <span class="s">"&lt;b&gt;"</span>:  <span class="c"># a &amp; b</span>'
    )
    assert lines[1] == '    x = <span class="n">10</span>'


def test_highlight_marks_defined_names_and_splits_multiline_strings() -> None:
    lines = highlight_lines(["def f():", '    """one', '    two"""', "    return 1"])
    assert lines[0] == '<span class="k">def</span> <span class="d">f</span>():'
    assert lines[1] == '    <span class="s">"""one</span>'
    assert lines[2] == '<span class="s">    two"""</span>'


def test_highlight_keeps_one_entry_per_line_including_blank_ones() -> None:
    assert highlight_lines(["x = 1", "", "y = 2"])[1] == ""


# --- summary contract ---


def test_summary_counts_judged_verdicts_and_leaves_unsupported_out() -> None:
    first, second, third = _mutants()[:3]
    results = [
        _result(first, MutantStatus.KILLED),
        _result(second, MutantStatus.SURVIVED),
        _result(third, MutantStatus.NO_COVERAGE),
        _result(first, MutantStatus.UNSUPPORTED),
    ]
    summary = build_summary(results, INFO)
    assert summary["counts"] == {"killed": 1, "survived": 1, "timeout": 0, "no_coverage": 1}
    assert summary["files"] == {PATH: summary["counts"]}
    assert summary["score"] == 0.5  # killed among covered: no-coverage is not in the denominator
    assert summary["generated_at"] == "2026-11-05T12:00:00+00:00"
    assert summary["history"] == []


def test_score_is_none_without_a_covered_mutant() -> None:
    assert mutation_score({"killed": 0, "survived": 0, "timeout": 2, "no_coverage": 5}) is None


def test_a_baseline_extends_the_history_by_itself_and_caps_it() -> None:
    mutant = _mutants()[0]
    baseline = build_summary([_result(mutant, MutantStatus.SURVIVED)], INFO)
    old_point: RunPoint = {
        "generated_at": "2026-10-01T09:00:00+00:00",
        "commit": "",
        "score": 0.1,
        "counts": baseline["counts"],
    }
    long_history: ReportSummary = {**baseline, "history": [old_point] * 50}

    later = replace(INFO, generated_at=INFO.generated_at + timedelta(days=1))
    summary = build_summary([_result(mutant, MutantStatus.KILLED)], later, long_history)

    assert len(summary["history"]) == 50
    assert summary["history"][-1] == {
        "generated_at": baseline["generated_at"],
        "commit": baseline["commit"],
        "score": 0.0,
        "counts": baseline["counts"],
    }


def test_the_embedded_summary_reads_back_as_a_baseline(tmp_path: Path) -> None:
    results = [_result(mutant, MutantStatus.KILLED) for mutant in _mutants()]
    report = tmp_path / "report.html"
    write_html(report, render_html(results, {PATH: SOURCE}, info=INFO))

    assert read_baseline(report) == build_summary(results, INFO)
    assert not list(tmp_path.glob(".*.tmp"))  # written whole, no leftover


def _valid_summary() -> Json:
    summary: Json = json.loads(json.dumps(build_summary([_result(_mutants()[0], MutantStatus.KILLED)], INFO)))
    return summary


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "balamut-report-summary/0"}, "unsupported summary schema"),
        ({"counts": {"killed": -1, "survived": 0, "timeout": 0, "no_coverage": 0}}, "non-negative integer"),
        ({"counts": {"killed": True, "survived": 0, "timeout": 0, "no_coverage": 0}}, "non-negative integer"),
        ({"score": 1.5}, "between 0 and 1"),
        ({"generated_at": "yesterday"}, "ISO 8601"),
        ({"scope": ["src", 3]}, "list of strings"),
        ({"history": {}}, "must be a list"),
        ({"files": {"a.py": {"killed": 1}}}, "non-negative integer"),
    ],
)
def test_a_malformed_summary_is_rejected(change: Json, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_summary({**_valid_summary(), **change})


def test_a_summary_with_a_null_score_parses() -> None:
    assert parse_summary({**_valid_summary(), "score": None})["score"] is None


def test_read_baseline_rejects_a_page_without_a_summary(tmp_path: Path) -> None:
    page = tmp_path / "other.html"
    page.write_text("<html><body>hello</body></html>", encoding="utf-8")
    with pytest.raises(ValueError, match="not a balamut HTML report"):
        read_baseline(page)


def test_read_baseline_rejects_broken_json_and_a_missing_file(tmp_path: Path) -> None:
    page = tmp_path / "broken.html"
    page.write_text('<script type="application/json" id="balamut-summary">{"schema": </script>', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_baseline(page)
    with pytest.raises(ValueError, match="could not read"):
        read_baseline(tmp_path / "missing.html")


# --- the page ---


def test_mutant_positions_are_character_columns_on_the_span_lines() -> None:
    source = 'def f(a: int, b: int) -> bool:\n    s = "привет"; return a < b\n'
    compare = next(m for m in _mutants(source) if m.original == "a < b")
    mutants = _file_mutants(render_html([_result(compare, MutantStatus.SURVIVED)], {PATH: source}, info=INFO))

    line = source.splitlines()[1]
    assert mutants[0]["line"] == 2
    assert mutants[0]["col"] == line.index("a < b")  # characters, not UTF-8 bytes
    assert mutants[0]["end_line"] == 2
    assert mutants[0]["end_col"] == line.index("a < b") + len("a < b")
    assert mutants[0]["bucket"] == "high"
    assert mutants[0]["covering"] == [0]


def test_a_killed_mutant_names_its_killer_and_a_timeout_its_reason() -> None:
    first, second, *_rest = _mutants()
    results = [
        _result(first, MutantStatus.KILLED, killed_by=("tests/t.py::test_kill",)),
        _result(second, MutantStatus.TIMEOUT, reason=VerdictReason.HANG_UNCONFIRMED),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)
    tests = _data(page)["tests"]
    mutants = _file_mutants(page)

    killed = next(m for m in mutants if m["status"] == "killed")
    timeout = next(m for m in mutants if m["status"] == "timeout")
    names = cast("list[str]", tests)
    assert [names[ref] for ref in cast("list[int]", killed["killed_by"])] == ["tests/t.py::test_kill"]
    assert killed["bucket"] == ""
    assert timeout["reason"] == "hang_unconfirmed"


def test_a_module_level_no_coverage_site_is_marked_cold() -> None:
    source = "class Model:\n    limit: int = 10\n"
    cold = next(m for m in _mutants(source) if m.operator.startswith("lang:const:num:"))
    page = render_html([_result(cold, MutantStatus.NO_COVERAGE, covering_tests=())], {PATH: source}, info=INFO)

    assert _file_mutants(page)[0]["cold"] == "schema_default"
    assert '<div class="tile-note">1 at import time</div>' in page


def test_source_text_cannot_close_the_data_script_early() -> None:
    source = 'def f() -> str:\n    return "</script><script>alert(1)</script>"\n'
    mutant = next(m for m in _mutants(source) if m.operator.startswith("lang:return"))
    page = render_html([_result(mutant, MutantStatus.SURVIVED)], {PATH: source}, info=replace(INFO, project="<x>"))

    assert page.count("</script>") == 3  # the summary, the data and the page script
    assert "<title>&lt;x&gt; mutation report</title>" in page
    assert "</script><script>alert(1)" not in page


def test_a_high_survivor_is_listed_with_a_link_to_its_line() -> None:
    compare = next(m for m in _mutants() if m.original == "age >= 18")
    page = render_html([_result(compare, MutantStatus.SURVIVED)], {PATH: SOURCE}, info=INFO)

    assert 'href="#f0-l3"' in page
    assert "Strengthen <code>tests/t.py</code>" in page


def test_without_a_baseline_the_page_invites_one_and_draws_no_trend() -> None:
    page = render_html([_result(_mutants()[0], MutantStatus.KILLED)], {PATH: SOURCE}, info=INFO)
    assert "--html-baseline" in page
    assert "progress-title" not in page


def test_with_a_baseline_the_page_compares_and_draws_the_trend() -> None:
    mutants = _mutants()
    before = build_summary([_result(m, MutantStatus.SURVIVED) for m in mutants[:2]], INFO)
    later = replace(INFO, generated_at=INFO.generated_at + timedelta(days=7))
    page = render_html(
        [_result(m, MutantStatus.KILLED) for m in mutants[:2]], {PATH: SOURCE}, info=later, baseline=before
    )

    assert "progress-title" in page
    assert "Mutation score over 2 runs, from 0.0% to 100.0%" in page
    assert "+100.0pp" in page


def test_an_empty_run_renders_a_page() -> None:
    page = render_html([], {}, info=INFO)
    assert "No high-priority survivors" in page
    assert _data(page)["files"] == []


# --- formatting, positions and deltas ---


def _by_original(original: str, source: str = SOURCE) -> Mutant:
    return next(m for m in _mutants(source) if m.original == original)


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0.4, "under 1 s"),
        (1, "1 s"),
        (59.4, "59 s"),
        (60, "1 min 00 s"),
        (3599, "59 min 59 s"),
        (3600, "1 h 00 min"),
        (3725, "1 h 02 min"),
    ],
)
def test_duration_reads_in_the_largest_fitting_unit(seconds: float, shown: str) -> None:
    assert _duration(seconds) == shown


def test_percentages_have_one_decimal_and_a_dash_for_none() -> None:
    assert _pct(0.12345) == "12.3%"
    assert _pct(1.0) == "100.0%"
    assert _pct(None) == "&mdash;"


def test_delta_states_direction_size_and_whether_it_helps() -> None:
    better_score = '<span class="delta up">&#9650; +7.0pp <em>better</em></span>'
    assert _delta(0.9, 0.83, unit="pp", higher_is_better=True, scale=100) == better_score
    assert (
        _delta(11, 10, unit="", higher_is_better=False) == '<span class="delta down">&#9650; +1 <em>worse</em></span>'
    )
    assert _delta(3, 55, unit="", higher_is_better=False) == (
        '<span class="delta up">&#9660; &minus;52 <em>better</em></span>'
    )
    assert _delta(40, 50, unit="", higher_is_better=True) == (
        '<span class="delta down">&#9660; &minus;10 <em>worse</em></span>'
    )


def test_a_delta_below_the_shown_precision_is_no_change() -> None:
    flat = '<span class="delta flat">no change</span>'
    assert _delta(5, 5, unit="", higher_is_better=False) == flat
    assert _delta(0.8004, 0.8, unit="pp", higher_is_better=True, scale=100) == flat
    assert _delta(0.8006, 0.8, unit="pp", higher_is_better=True, scale=100) == (
        '<span class="delta up">&#9650; +0.1pp <em>better</em></span>'
    )


def test_script_json_is_compact_unicode_and_cannot_close_a_script() -> None:
    assert _script_json({"a": "</script>", "b": "é"}) == '{"a":"\\u003c/script>","b":"é"}'


def test_source_lines_follow_the_tokenizer_line_breaks() -> None:
    source = "a = 1\r\nb = 2\rc = 'é'\n"
    text = _Source.of(source)
    assert text.lines == ["a = 1", "b = 2", "c = 'é'"]
    assert text.position(len("a = 1\r\nb = 2\rc = 'é".encode())) == (3, 6)  # characters, not bytes


def test_source_lines_keep_a_last_line_without_a_line_break() -> None:
    assert _Source.of("x = 1\ny = 2").lines == ["x = 1", "y = 2"]
    assert _Source.of("x = 1\n").lines == ["x = 1"]  # a final line break opens no line
    assert _Source.of("").lines == [""]


def test_a_span_ending_at_a_line_start_closes_the_line_before_it() -> None:
    text = _Source.of("x = 10\ny = 2\n")
    assert text.span(0, 7) == (1, 0, 1, 6)
    assert text.span(2, 9) == (1, 2, 2, 2)
    assert text.span(7, 7) == (2, 0, 2, 0)  # an insertion keeps its point


def test_highlight_marks_class_names_and_only_the_name_after_def() -> None:
    lines = highlight_lines(["class C(B):", "    x = f(y)"])
    assert lines[0] == '<span class="k">class</span> <span class="d">C</span>(B):'
    assert lines[1] == "    x = f(y)"


# --- the summary page ---


def test_timeouts_count_but_stay_out_of_the_score() -> None:
    killed, survived, other = _mutants()[:3]
    results = [
        _result(killed, MutantStatus.KILLED),
        _result(survived, MutantStatus.SURVIVED),
        _result(other, MutantStatus.SURVIVED),
        _result(killed, MutantStatus.TIMEOUT),
    ]
    summary = build_summary(results, INFO)
    assert summary["counts"] == {"killed": 1, "survived": 2, "timeout": 1, "no_coverage": 0}
    assert summary["score"] == 0.3333


def test_the_hero_states_score_reach_and_catch() -> None:
    results = [
        _result(_by_original("return age >= 18"), MutantStatus.KILLED),
        _result(_by_original("age >= 18"), MutantStatus.SURVIVED),
        _result(_by_original("18"), MutantStatus.NO_COVERAGE, covering_tests=()),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)

    assert '<div class="score-value">50.0%</div>' in page
    assert (
        "balamut planted <strong>3</strong> small faults in <strong>1</strong> file. The tests reached "
        "<strong>2</strong> of them (66.7%) and caught <strong>1</strong>."
    ) in page
    assert '<div class="tile-value">1</div><div class="tile-note">1 high priority</div>' in page
    assert '<div class="tile-note">no test runs this code</div>' in page
    assert "<dt>Run time</dt><dd>42 s</dd>" in page
    assert "<dt>Revision</dt><dd><code>main</code> &middot; <code>abc123def456</code></dd>" in page
    assert '<canvas id="mosaic" role="img" aria-label="3 mutants by verdict, in file and line order">' in page


def test_the_hero_counts_files_and_shows_the_changes_against_the_baseline() -> None:
    other = "src/pkg/other.py"
    before = build_summary([_result(_by_original("age >= 18"), MutantStatus.SURVIVED)], INFO)
    results = [
        _result(_by_original("age >= 18"), MutantStatus.KILLED),
        _result(_mutants(SOURCE, other)[1], MutantStatus.KILLED),
    ]
    page = render_html(results, {PATH: SOURCE, other: SOURCE}, info=INFO, baseline=before)

    assert "small faults in <strong>2</strong> files." in page
    assert '<span class="delta up">&#9650; +100.0pp <em>better</em></span><span class="since">since ' in page
    assert (
        '<div class="tile-note">a test failed on them</div><span class="delta up">&#9650; +2 <em>better</em></span>'
    ) in page


def test_the_progress_table_compares_each_count_and_notes_another_scope() -> None:
    before = build_summary(
        [_result(m, MutantStatus.SURVIVED) for m in _mutants()],
        replace(INFO, scope=("src/other",), kinds=("src", "extreme")),
    )
    results = [_result(m, MutantStatus.KILLED) for m in _mutants()[:2]] + [
        _result(_mutants()[2], MutantStatus.SURVIVED)
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO, baseline=before)

    assert (
        '<tr><th scope="row">Survived</th><td class="num">3</td><td class="num">1</td>'
        '<td><span class="delta up">&#9660; &minus;2 <em>better</em></span></td></tr>'
    ) in page
    assert (
        '<tr><th scope="row">Mutation score</th><td class="num">0.0%</td><td class="num">66.7%</td>'
        '<td><span class="delta up">&#9650; +66.7pp <em>better</em></span></td></tr>'
    ) in page
    assert '<tr><th scope="row">Mutants</th><td class="num">3</td><td class="num">3</td><td></td></tr>' in page
    assert "another scope (src/other; kinds src, extreme)" in page


def _point(generated_at: str, score: float | None, commit: str = "") -> RunPoint:
    return {
        "generated_at": generated_at,
        "commit": commit,
        "score": score,
        "counts": {"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0},
    }


def test_the_trend_draws_every_scored_run_on_one_scale() -> None:
    svg = _trend(
        [
            _point("2026-11-01T10:00:00+00:00", 0.5, "aaa"),
            _point("2026-11-03T10:00:00+00:00", None),
            _point("2026-11-04T10:00:00+00:00", 0.83),
            _point("2026-11-05T10:00:00+00:00", 0.9),
        ]
    )

    assert 'points="44.0,184.0 330.0,73.1 616.0,49.6"' in svg
    assert 'points="44,184 44.0,184.0 330.0,73.1 616.0,49.6 616.0,184"' in svg
    assert re.findall(r'text-anchor="end">(\d+)%</text>', svg) == ["50", "60", "70", "80", "90", "100"]
    assert re.findall(r'text-anchor="middle">([^<]+)</text>', svg) == ["01 Nov", "04 Nov", "05 Nov"]
    assert 'class="end-label" x="606.0" y="37.6" text-anchor="end">90.0%</text>' in svg
    assert 'data-tip="01 Nov 2026, 10:00: 50.0% at aaa"' in svg
    assert 'aria-label="Mutation score over 3 runs, from 50.0% to 90.0%"' in svg


def test_a_narrow_trend_ticks_every_five_points_and_same_day_runs_show_times() -> None:
    svg = _trend([_point("2026-11-05T09:30:00+00:00", 0.85), _point("2026-11-05T17:45:00+00:00", 0.95)])
    assert re.findall(r'text-anchor="end">(\d+)%</text>', svg) == ["80", "85", "90", "95", "100"]
    assert re.findall(r'text-anchor="middle">([^<]+)</text>', svg) == ["09:30", "17:45"]


def test_a_perfect_trend_keeps_a_floor_below_the_top() -> None:
    svg = _trend([_point("2026-11-01T10:00:00+00:00", 1.0), _point("2026-11-02T10:00:00+00:00", 1.0)])
    assert re.findall(r'text-anchor="end">(\d+)%</text>', svg) == ["90", "95", "100"]


def test_a_trend_needs_two_scored_runs() -> None:
    assert _trend([_point("2026-11-01T10:00:00+00:00", 0.5), _point("2026-11-02T10:00:00+00:00", None)]) == ""


def test_the_files_table_carries_sort_values_and_baseline_changes() -> None:
    other = "src/other.py"
    before = build_summary([_result(_by_original("age >= 18"), MutantStatus.SURVIVED)], INFO)
    results = [
        _result(_by_original("age >= 18"), MutantStatus.KILLED),
        _result(_by_original("18"), MutantStatus.SURVIVED),
        _result(_by_original("return age >= 18"), MutantStatus.TIMEOUT),
        _result(_mutants(SOURCE, other)[0], MutantStatus.NO_COVERAGE, covering_tests=()),
    ]
    page = render_html(results, {PATH: SOURCE, other: SOURCE}, info=INFO, baseline=before)

    assert (
        '<tr data-path="src/pkg/calc.py" data-total="3" data-score="0.500000" data-killed="1" data-survived="1" '
        'data-no_coverage="0" data-timeout="1" data-delta="50.0000">'
        '<td class="path"><a href="#f1"><span class="dir">src/pkg/</span><span class=\'name\'>calc.py</span></a></td>'
    ) in page
    assert (
        '<td class="num strong">50.0%</td><td class="num">3</td><td class="num">1</td><td class="num hot">1</td>'
        in (page)
    )
    assert 'data-score="" data-killed="0" data-survived="0" data-no_coverage="1" data-timeout="0" data-delta="">' in (
        page
    )
    assert '<td class="num">1</td><td class="num">0</td><td><span class="delta flat">new</span></td></tr>' in page


def test_breakdowns_order_groups_by_size_and_score_each() -> None:
    first, second, third = _mutants()[:3]
    results = [
        _result(first, MutantStatus.KILLED),
        _result(second, MutantStatus.SURVIVED),
        _result(replace(third, family="extreme"), MutantStatus.KILLED),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)

    assert re.findall(r'<span class="bd-name"><code>(\w+)</code>', page) == ["src", "extreme"]
    assert '<span class="bd-score">50.0%</span><span class="bd-n">2</span>' in page
    assert '<span class="bd-score">100.0%</span><span class="bd-n">1</span>' in page
    assert 'class="breakdowns single"' in page  # no risk area without a tagged mutant


def test_a_tagged_mutant_opens_the_risk_area_breakdown() -> None:
    source = "def owns(row_tenant_id: int, tenant_id: int) -> bool:\n    return row_tenant_id == tenant_id\n"
    compare = _by_original("row_tenant_id == tenant_id", source)
    page = render_html([_result(compare, MutantStatus.SURVIVED)], {PATH: source}, info=INFO)

    assert "<h3>By risk area</h3>" in page
    assert '<span class="bd-name"><code>tenant</code></span>' in page
    assert _file_mutants(page)[0]["semantic"] == ["tenant"]
    assert '<span class="tag sem">tenant</span>' in page


def test_a_large_survivor_group_lists_six_sites_per_file_and_counts_the_rest() -> None:
    source = "".join(f"def f{index}(a: int, b: int) -> bool:\n    return a < b\n\n\n" for index in range(8))
    survivors = [_result(m, MutantStatus.SURVIVED) for m in _mutants(source) if m.original == "a < b"]
    page = render_html(survivors, {PATH: source}, info=INFO)

    assert page.count('class="site" href="#f0-l') == 6
    assert '<span class="more">+2 more</span>' in page
    assert f'<div class="cl-count">{len(survivors)}<span>&times;</span></div>' in page


def test_a_survivor_that_deletes_code_reads_as_removed() -> None:
    deleted = replace(_by_original("age >= 18"), mutated="")
    page = render_html([_result(deleted, MutantStatus.SURVIVED)], {PATH: SOURCE}, info=INFO)
    assert '<span class="smp-gone">removed</span>' in page


def test_a_body_no_coverage_site_is_not_cold() -> None:
    page = render_html([_result(_mutants()[1], MutantStatus.NO_COVERAGE, covering_tests=())], {PATH: SOURCE}, info=INFO)
    assert _file_mutants(page)[0]["cold"] == ""


def test_the_footer_counts_the_carried_history() -> None:
    mutant = _mutants()[0]
    first = build_summary([_result(mutant, MutantStatus.SURVIVED)], INFO)
    second = build_summary([_result(mutant, MutantStatus.KILLED)], INFO, first)
    one = render_html([_result(mutant, MutantStatus.KILLED)], {PATH: SOURCE}, info=INFO, baseline=first)
    two = render_html([_result(mutant, MutantStatus.KILLED)], {PATH: SOURCE}, info=INFO, baseline=second)

    assert "This report carries the history of 1 earlier run.</p>" in one
    assert "This report carries the history of 2 earlier runs.</p>" in two


def test_equal_groups_break_ties_by_name() -> None:
    first, second = _mutants()[:2]
    results = [
        _result(replace(first, family="src"), MutantStatus.KILLED),
        _result(replace(second, family="extreme"), MutantStatus.KILLED),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)
    assert re.findall(r'<span class="bd-name"><code>(\w+)</code>', page) == ["extreme", "src"]


def test_the_project_semantic_patterns_tag_the_mutants() -> None:
    compare = _by_original("age >= 18")
    page = render_html(
        [_result(compare, MutantStatus.SURVIVED)], {PATH: SOURCE}, info=INFO, semantic=(("majority", ("age",)),)
    )
    assert _file_mutants(page)[0]["semantic"] == ["majority"]


def test_only_surviving_high_priority_mutants_count_as_high() -> None:
    results = [
        _result(_by_original("return age >= 18"), MutantStatus.SURVIVED),
        _result(_by_original("age >= 18"), MutantStatus.SURVIVED),
        _result(_by_original("18"), MutantStatus.SURVIVED),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)
    assert '<div class="tile-note">2 high priority</div>' in page
    assert "<p>2 high-priority survivors in 2 groups." in page


def test_unsupported_mutants_are_named_apart_from_the_counts() -> None:
    first, second = _mutants()[:2]
    results = [_result(first, MutantStatus.KILLED), _result(second, MutantStatus.UNSUPPORTED)]
    page = render_html(results, {PATH: SOURCE}, info=INFO)
    assert "1 more mutants could not be applied to the running code" in page


def test_verdict_bars_name_each_part_and_its_share() -> None:
    results = [
        _result(_by_original("return age >= 18"), MutantStatus.KILLED),
        _result(_by_original("age >= 18"), MutantStatus.SURVIVED),
        _result(_by_original("18"), MutantStatus.NO_COVERAGE, covering_tests=()),
    ]
    page = render_html(results, {PATH: SOURCE}, info=INFO)
    assert (
        '<span class="seg" role="img" aria-label="src/pkg/calc.py: Killed 1, Survived 1, Timeout 0, No coverage 1">'
        '<span class="seg-part v-killed" style="flex-grow:1" data-tip="Killed: 1 of 3 (33%)"></span>'
    ) in page


def test_the_page_inlines_its_styles_and_script() -> None:
    page = render_html([], {}, info=INFO)
    assert "<style>\n/* Layout: an assay sheet." in page
    assert 'window.addEventListener("hashchange", route);' in page


def test_long_survivor_samples_are_cut_to_a_readable_length() -> None:
    long = replace(_by_original("age >= 18"), original="x" * 130, mutated="y" * 130)
    page = render_html([_result(long, MutantStatus.SURVIVED)], {PATH: SOURCE}, info=INFO)
    assert f'<span class="smp-del">{"x" * 120}</span>' in page
    assert f'<span class="smp-add">{"y" * 120}</span>' in page
