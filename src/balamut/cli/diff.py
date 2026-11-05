"""``balamut diff``: the diff mutation gate, and ``balamut doctor``, its read-only check.

One run pins the scope (the diff against a merge base, with content hashes), runs the project's preflight,
reuses or builds the coverage substrate (from the shared pool when this tree has none), runs ``balamut mutate``
over the scope in a subprocess and gates its verdicts. Every terminal state writes one JSON report.

Exit codes: 0 CLEAN; 1 FINDINGS or UNRESOLVED; 3 SCOPE_CHANGED; 2 everything else (baseline or infrastructure).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from importlib.metadata import entry_points
from pathlib import Path

from balamut import __version__
from balamut.cli.exit_codes import EXIT_REFRESH_BASELINE_FAILURE, EXIT_SCOPE_CHANGED
from balamut.config import SINK_ENTRY_POINT_GROUP, Layout, Profile, ProfileError, load_profile
from balamut.coverage import baseline_failure, failures, pool, substrate
from balamut.coverage import costs as pytest_costs
from balamut.coverage.measure import MeasureRequest, MeasureResult, measure_coverage
from balamut.diff import gate as diff_gate
from balamut.diff import scope as diff_scope
from balamut.engine.stats import CAMPAIGN_TIMING_KEYS, CampaignStats, read_campaign_stats
from balamut.isolation import Environ, Isolation, IsolationError, isolation_for
from balamut.paths import default_address, ensure_state_dir, substrate_path

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]
type JsonMapping = Mapping[str, JsonValue]
type Findings = list[JsonObject]
type Counts = dict[str, int]
type Timings = dict[str, float]
type FingerprintEnvironment = dict[str, str]
type CommandRunner = Callable[[CommandInvocation], int]
type Measurer = Callable[[MeasureRequest], MeasureResult]
type Prober = Callable[[], None]
type ScopeResolver = Callable[[str, diff_scope.BaseMode], diff_scope.DiffScope]
type ScopeWriter = Callable[[diff_scope.DiffScope, Path], None]
type ScopeChangesResolver = Callable[[diff_scope.DiffScope, Path], diff_scope.ScopeChanges]
type SubstrateInspector = Callable[[Path], substrate.SubstrateMetadata | None]
type SubstrateEnsurer = Callable[
    [
        Path,
        substrate.SubstrateMetadata,
        substrate.SubstrateBuilder,
        SubstrateBuildMode,
        substrate.TreeSnapshotter,
        substrate.UnmeasuredFiles,
    ],
    substrate.SubstrateMetadata,
]
# (repository root, substrate path, expected metadata, current tree, diff files) -> the seeded generation
type SubstrateSeeder = Callable[
    [Path, Path, substrate.SubstrateMetadata, substrate.TreeRecord, frozenset[str]], substrate.SubstrateMetadata | None
]
type SubstrateSharer = Callable[[Path, Path, substrate.SubstrateMetadata], None]
type Clock = Callable[[], float]
type ReportSink = Callable[[JsonMapping], None]
type NamedSink = tuple[str, ReportSink]
type _FindingSite = tuple[str, int]
type _FindingOutcome = tuple[str, str, str]
type _SiteOutcomes = dict[_FindingSite, list[_FindingOutcome]]

_STATUSES = ("killed", "survived", "timeout", "no_coverage", "unsupported")
REPORT_TIMING_KEYS = ("preflight", "substrate", *CAMPAIGN_TIMING_KEYS, "mutation", "gate")
_DETAIL_PATHS = 10
_RUNTIME_DISTRIBUTIONS = ("coverage", "pytest", "pytest-fast")


class SubstrateBuildMode(StrEnum):
    REUSE = "reuse"
    REBUILD = "rebuild"


class RunStatus(StrEnum):
    """The terminal status of one run."""

    CLEAN = "CLEAN"
    FINDINGS = "FINDINGS"
    UNRESOLVED = "UNRESOLVED"
    BASELINE_FAILURE = "BASELINE_FAILURE"
    INFRA_FAILURE = "INFRA_FAILURE"
    SCOPE_CHANGED = "SCOPE_CHANGED"


class DiffMode(StrEnum):
    """How deep the gate mutates the changed files."""

    FAST = "fast"
    FULL = "full"


class FailureStage(StrEnum):
    """The phase whose failure ended the run: what the report names as the failure stage."""

    SCOPE = "scope"
    PREFLIGHT = "preflight"
    SUBSTRATE = "substrate"
    MUTATION = "mutation"
    COVERAGE_REFRESH = "coverage_refresh"
    GATE = "gate"
    ORCHESTRATION = "orchestration"
    REPORT = "report"


def _empty_environment() -> Environ:
    return {}


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """One external process: its argv, extra environment and an optional stdout file."""

    argv: tuple[str, ...]
    environment: Environ = field(default_factory=_empty_environment)
    stdout_path: Path | None = None


@dataclass(frozen=True, slots=True)
class DiffRuntime:
    """The external boundaries of the orchestrator, replaceable in tests."""

    command_run: CommandRunner
    measure: Measurer
    isolation_probe: Prober
    scope_resolve: ScopeResolver
    scope_write: ScopeWriter
    scope_changes: ScopeChangesResolver
    substrate_inspect: SubstrateInspector
    substrate_ensure: SubstrateEnsurer
    # The pool shared by every worktree of the repository: a seed instead of a build, and the publication of a build.
    substrate_seed: SubstrateSeeder
    substrate_share: SubstrateSharer
    clock: Clock
    report_sink: ReportSink


@dataclass(frozen=True, slots=True)
class RunOptions:
    base: str
    base_mode: diff_scope.BaseMode
    mode: DiffMode
    coverage_path: Path
    raw_json_path: Path
    report_json_path: Path
    agent_json: bool
    rebuild_substrate: bool
    mutate_args: tuple[str, ...]


def _initial_counts() -> Counts:
    return dict.fromkeys(("enumerated", "cache_resolved", "executed", *_STATUSES), 0)


def _initial_timings() -> Timings:
    return dict.fromkeys(REPORT_TIMING_KEYS, 0.0)


@dataclass(slots=True)
class RunState:
    scope: diff_scope.DiffScope | None = None
    substrate_generation: str = ""
    substrate_reused: bool = False
    substrate_miss_reason: str | None = None
    substrate_drift_files: int = 0  # diverged outside the diff from the build tree; the refresh re-measures them
    substrate_seeded_from: str | None = None  # the pooled generation taken instead of a build
    failure_stage: FailureStage | None = None
    error_code: str | None = None
    error_detail: str | None = None
    baseline_failure_cached: bool = False
    baseline_failure_tests: list[str] = field(default_factory=list[str])
    scope_drift: list[str] = field(default_factory=list[str])
    invocation: JsonObject = field(default_factory=dict[str, JsonValue])
    evaluations: list[JsonObject] = field(default_factory=list[JsonObject])
    counts: Counts = field(default_factory=_initial_counts)
    timings: Timings = field(default_factory=_initial_timings)


def _command_run(invocation: CommandInvocation, *, cwd: Path) -> int:
    environment = {**os.environ, **invocation.environment}
    if invocation.stdout_path is not None:
        with invocation.stdout_path.open("w", encoding="utf-8") as stdout_file:
            return subprocess.run(
                list(invocation.argv), cwd=cwd, env=environment, stdout=stdout_file, text=True, check=False
            ).returncode
    return subprocess.run(
        list(invocation.argv), cwd=cwd, env=environment, stdout=sys.stderr, text=True, check=False
    ).returncode


def _substrate_ensure(
    coverage_path: Path,
    expected: substrate.SubstrateMetadata,
    build: substrate.SubstrateBuilder,
    build_mode: SubstrateBuildMode,
    tree: substrate.TreeSnapshotter,
    unmeasured: substrate.UnmeasuredFiles,
) -> substrate.SubstrateMetadata:
    return substrate.substrate_ensure(
        coverage_path,
        expected,
        build=build,
        force_rebuild=build_mode is SubstrateBuildMode.REBUILD,
        tree=tree,
        unmeasured=unmeasured,
    )


def _substrate_seed(
    layout: Layout,
    repo_root: Path,
    coverage_path: Path,
    expected: substrate.SubstrateMetadata,
    current: substrate.TreeRecord,
    in_diff: frozenset[str],
) -> substrate.SubstrateMetadata | None:
    """A seed from the pool is a speedup, not a condition: a failure to read the pool leaves an ordinary build."""
    try:
        shared = pool.pool_dir(repo_root)
        if shared is None:
            return None
        return pool.pool_seed(shared, coverage_path, expected, current, in_diff=in_diff, layout=layout)
    except OSError as exc:
        sys.stderr.write(f"balamut diff: the shared substrate pool is unavailable, building from scratch: {exc}\n")
        return None


def _substrate_share(repo_root: Path, coverage_path: Path, published: substrate.SubstrateMetadata) -> None:
    """Sharing never affects this run's verdict: a failure is a warning."""
    try:
        shared = pool.pool_dir(repo_root)
        if shared is not None:
            pool.pool_publish(shared, coverage_path, published)
    except OSError as exc:
        sys.stderr.write(f"balamut diff: the build did not reach the shared substrate pool: {exc}\n")


def load_report_sinks() -> list[NamedSink]:
    """Report sinks installed as ``balamut.sinks`` entry points: callables receiving every terminal report."""
    sinks: list[NamedSink] = []
    for point in entry_points(group=SINK_ENTRY_POINT_GROUP):
        sink: ReportSink = point.load()
        sinks.append((point.name, sink))
    return sinks


def entry_point_sink(report: JsonMapping) -> None:
    """Hand the report to every installed sink; a failing sink never changes the run's result or stdout."""
    for name, sink in load_report_sinks():
        try:
            sink(report)
        except Exception as exc:  # a third-party sink: any failure is its own, reported and contained
            sys.stderr.write(f"warning: report sink {name} failed: {exc}\n")


def runtime_dependencies(profile: Profile, isolation: Isolation) -> DiffRuntime:
    """The boundaries of a real local run."""
    root = profile.root
    return DiffRuntime(
        command_run=partial(_command_run, cwd=root),
        measure=partial(measure_coverage, isolation=isolation),
        isolation_probe=isolation.probe,
        scope_resolve=partial(_scope_resolve, root),
        scope_write=diff_scope.write_scope,
        scope_changes=_scope_changes,
        substrate_inspect=substrate.substrate_inspect,
        substrate_ensure=_substrate_ensure,
        substrate_seed=partial(_substrate_seed, profile.layout),
        substrate_share=_substrate_share,
        clock=time.monotonic,
        report_sink=entry_point_sink,
    )


def _scope_resolve(root: Path, base_ref: str, base_mode: diff_scope.BaseMode) -> diff_scope.DiffScope:
    return diff_scope.resolve_scope(base_ref, repo_root=root, base_mode=base_mode)


def _scope_changes(scope: diff_scope.DiffScope, repo_root: Path) -> diff_scope.ScopeChanges:
    return diff_scope.scope_changes(scope, repo_root=repo_root)


def _fingerprint(entries: Sequence[tuple[str, str]]) -> str:
    payload = json.dumps(list(entries), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_entry(root: Path, relative: str) -> tuple[str, str]:
    path = root / relative
    return (relative, substrate.file_sha256(path)) if path.is_file() else (relative, "missing")


def config_fingerprint(profile: Profile) -> str:
    """Everything that shapes how coverage is measured: the project's pytest and coverage configuration files, the
    layout, the coverage settings and the balamut version."""
    layout = profile.layout
    entries = [_file_entry(profile.root, relative) for relative in profile.substrate_inputs]
    entries += [
        ("balamut", __version__),
        ("substrate_schema", str(substrate.SUBSTRATE_SCHEMA_VERSION)),
        ("source", ",".join(layout.source)),
        ("import_roots", ",".join(layout.import_roots)),
        ("tests", ",".join(layout.tests)),
        ("test_patterns", ",".join(layout.test_patterns)),
        ("concurrency", ",".join(profile.coverage.concurrency)),
    ]
    return _fingerprint(entries)


def inventory_fingerprint(layout: Layout) -> str:
    """The set of collectable test files, not their bodies.

    An edited test is refreshed precisely (its file is in the diff) and a test that went red is named by the refresh
    as a baseline failure; a fingerprint of bodies would rebuild the substrate on every test edit, which is exactly
    what closing a finding takes. The set still matters: a rebase may bring a test file outside the diff."""
    files: set[str] = set()
    for test_root in layout.tests:
        base = layout.root / test_root
        if base.is_dir():
            files.update(
                path.relative_to(layout.root).as_posix()
                for path in base.rglob("*.py")
                if layout.is_test_file(path.name)
            )
    return _fingerprint([(path, "collectable") for path in sorted(files)])


def runtime_fingerprint() -> str:
    """Python and the versions of the measuring components."""
    entries: list[tuple[str, str]] = [
        ("python", platform.python_version()),
        ("implementation", sys.implementation.name),
        ("cache_tag", sys.implementation.cache_tag or ""),
        ("machine", platform.machine()),
    ]
    for distribution in _RUNTIME_DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "missing"
        entries.append((distribution, version))
    return _fingerprint(entries)


def expected_metadata(profile: Profile, isolation: Isolation) -> substrate.SubstrateMetadata:
    """The metadata a substrate built now would carry, from its deterministic inputs only."""
    return substrate.SubstrateMetadata(
        schema_version=substrate.SUBSTRATE_SCHEMA_VERSION,
        complete=False,
        generation="",
        config_fingerprint=config_fingerprint(profile),
        test_inventory_fingerprint=inventory_fingerprint(profile.layout),
        runtime_fingerprint=runtime_fingerprint(),
        schema_fingerprint=isolation.fingerprint(),
        coverage_sha256="",
    )


def fingerprint_environment(profile: Profile, isolation: Isolation, environ: Environ) -> FingerprintEnvironment:
    """The environment a baseline confirmation depends on: the isolation and the configured database target.

    Values (secrets included) only enter a hash; the record never stores them."""
    environment = {"isolation": isolation.fingerprint()}
    names = profile.isolation.env
    if names is not None:
        for name in (names.host, names.port, names.database, names.user, names.password):
            environment[name] = environ.get(name, "")
    return environment


def _paths_brief(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_DETAIL_PATHS])
    rest = len(paths) - _DETAIL_PATHS
    return shown + (f" (+{rest} more)" if rest > 0 else "")


def build_estimate(costs_file: Path, workers: int) -> float | None:
    """The duration of a full build from the test costs of the previous build of this substrate; None without one.

    The sidecar holds the full duration of every test under the same coverage, so their sum over the build workers
    stays close to the real wall: the caller knows how long to wait and does not mistake a long build for a hang."""
    costs = pytest_costs.read_costs(costs_file)
    if not costs or workers <= 0:
        return None
    return sum(costs.values()) / workers


def _build_banner(miss_reason: str | None, estimate: float | None) -> str:
    expected = f", ~{max(1, round(estimate / 60))} min by the last build's costs" if estimate else ""
    return f"balamut diff: full coverage substrate build (reason: {miss_reason or 'rebuild'}){expected}\n"


def _scope_change_detail(changes: diff_scope.ScopeChanges) -> str:
    if changes.paths:
        return f"changed during run: {_paths_brief(changes.paths)}"
    if changes.current:
        # The engine saw the edit and stopped the campaign; the content was restored by the final check.
        return "changed during run: content restored before the final check"
    return "scope state unreadable"


def _report_scope(scope: diff_scope.DiffScope | None) -> JsonObject:
    if scope is None:
        return {"base_sha": "", "diff_base_sha": "", "head_sha": ""}
    return {"base_sha": scope.base_sha, "diff_base_sha": scope.diff_base_sha, "head_sha": scope.head_sha}


def report_build(
    state: RunState, status: RunStatus, *, findings: Findings | None = None, next_action: str | None = None
) -> JsonObject:
    serialized: list[JsonValue] = [*(findings or [])]
    drift: list[JsonValue] = [*state.scope_drift]
    tests: list[JsonValue] = [*state.baseline_failure_tests]
    evaluations: list[JsonValue] = [dict(evaluation) for evaluation in state.evaluations]
    return {
        "status": status.value,
        "scope": _report_scope(state.scope),
        "scope_drift": drift,
        "substrate": {
            "generation": state.substrate_generation,
            "reused": state.substrate_reused,
            "miss_reason": state.substrate_miss_reason,
            "drift_files": state.substrate_drift_files,
            "seeded_from": state.substrate_seeded_from,
        },
        "invocation": dict(state.invocation),
        "counts": {**state.counts},
        "baseline_failure": {"cached": state.baseline_failure_cached, "tests": tests},
        "findings": serialized,
        "evaluations": evaluations,
        "timings": {name: round(seconds, 6) for name, seconds in state.timings.items()},
        "failure_stage": state.failure_stage,
        "error_code": state.error_code,
        "error_detail": state.error_detail,
        "next_action": next_action,
    }


def failure_report(
    state: RunState,
    status: RunStatus,
    *,
    stage: FailureStage,
    code: str,
    next_action: str,
    findings: Findings | None = None,
) -> JsonObject:
    state.failure_stage = stage
    state.error_code = code
    return report_build(state, status, findings=findings, next_action=next_action)


def _scope_changed_report(state: RunState, changes: diff_scope.ScopeChanges) -> JsonObject:
    state.scope_drift = list(changes.moved_refs)
    state.error_detail = _scope_change_detail(changes)
    return failure_report(
        state,
        RunStatus.SCOPE_CHANGED,
        stage=FailureStage.SCOPE,
        code="scope_changed",
        next_action="restart_with_fresh_scope",
    )


def report_wall_seconds(report: JsonMapping) -> float:
    """The wall of a run: the sum of its top-level stages (campaign stages live inside ``mutation``)."""
    timings = report.get("timings")
    if not isinstance(timings, dict):
        return 0.0
    return float(
        sum(
            value
            for key, value in timings.items()
            if key not in CAMPAIGN_TIMING_KEYS and isinstance(value, int | float) and not isinstance(value, bool)
        )
    )


def report_write(path: Path, report: JsonMapping) -> None:
    """Atomically publish the final JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _mutation_stats(path: Path) -> CampaignStats:
    stats = read_campaign_stats(path)
    if stats is None or not stats.complete:
        raise ValueError("the campaign stats snapshot is missing, incomplete or inconsistent")
    return stats


def _finding(mutant: diff_gate.Mutant) -> JsonObject:
    if not mutant.key:
        raise ValueError("an actionable finding has no key")
    covering: list[JsonValue] = [*mutant.covering_tests]
    return {
        "key": mutant.key,
        "path": mutant.path,
        "line": mutant.lineno,
        "span": {"start": mutant.site_lineno, "end": mutant.site_end_lineno},
        "operator": mutant.operator,
        "status": mutant.status.value,
        "bucket": mutant.bucket.value,
        "reason": mutant.reason,
        "original": mutant.original,
        "mutated": mutant.mutated,
        "covering_tests": covering,
    }


def _stats_apply(state: RunState, stats: CampaignStats) -> None:
    state.counts.update(
        {
            "enumerated": stats.enumerated,
            "cache_resolved": stats.cache_resolved,
            "executed": stats.executed,
            **{status: stats.status_counts[status] for status in _STATUSES},
        }
    )
    state.timings.update(stats.timings)


def _raw_validate(mutants: list[diff_gate.Mutant], stats: CampaignStats) -> None:
    if len(mutants) != stats.enumerated:
        raise ValueError("the raw mutation JSON does not match the enumerated count")
    raw_counts = Counter(mutant.status.value for mutant in mutants)
    if any(raw_counts.get(status, 0) != stats.status_counts[status] for status in _STATUSES):
        raise ValueError("the raw mutation JSON does not match the status counts")


def _invocation_report(options: RunOptions) -> JsonObject:
    encoded = json.dumps(options.mutate_args, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "mode": options.mode.value,
        "base_mode": options.base_mode.value,
        "base_ref": options.base,
        "mutate_args_digest": hashlib.sha256(encoded).hexdigest(),
    }


def substrate_miss_reason(
    coverage_path: Path,
    previous: substrate.SubstrateMetadata | None,
    expected: substrate.SubstrateMetadata,
    *,
    force_rebuild: bool,
) -> str | None:
    if force_rebuild:
        return "forced_rebuild"
    if previous is not None:
        # Neither the test inventory nor test support is here: a new test file or a helper edit is a divergence from
        # the build tree, re-measured by the refresh.
        mismatches = substrate.metadata_mismatches(previous, expected)
        return mismatches[0] if mismatches else None
    return substrate_integrity_issue(coverage_path)


def substrate_integrity_issue(coverage_path: Path) -> str:
    """The first breakage of the substrate on disk; ``invalid_metadata`` when the files are intact but unreadable.

    One check for both the cache miss and the doctor, so both name a breakage the same way. The coverage checksum is
    checked before the cost sidecar: replaced coverage is worse than a missing sidecar."""
    if not coverage_path.is_file():
        return "missing_coverage"
    metadata_file = substrate.metadata_path(coverage_path)
    if not metadata_file.is_file():
        return "missing_metadata"
    try:
        raw: JsonValue = json.loads(metadata_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return "invalid_metadata"
        coverage_checksum = raw.get("coverage_sha256")
        if not isinstance(coverage_checksum, str) or substrate.file_sha256(coverage_path) != coverage_checksum:
            return "coverage_checksum"
        cost_file = substrate.costs_path(coverage_path)
        if not cost_file.is_file():
            return "missing_costs"
        costs_checksum = raw.get("costs_sha256")
        if not isinstance(costs_checksum, str) or substrate.file_sha256(cost_file) != costs_checksum:
            return "costs_checksum"
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return "invalid_metadata"
    return "invalid_metadata"


def _evaluation_row(mutant: diff_gate.Mutant) -> JsonObject:
    """The evaluation of a scope mutant with its site: the line of the edit (``site_lineno``), not the body line
    covering tests are selected by."""
    return {
        "key": mutant.key,
        "status": mutant.status.value,
        "path": mutant.path,
        "line": mutant.site_lineno,
        "operator": mutant.operator,
        "bucket": mutant.bucket.value,
        "original": mutant.original,
        "mutated": mutant.mutated,
    }


def _scoped_evaluations(mutants: list[diff_gate.Mutant], scope: diff_scope.DiffScope) -> list[JsonObject]:
    rows = [
        _evaluation_row(mutant)
        for mutant in mutants
        if mutant.key
        and diff_scope.mutant_in_scope(mutant.path, mutant.site_lineno, mutant.site_end_lineno, scope.changed_lines)
    ]
    return sorted(rows, key=lambda row: str(row["key"]))


def _confirmation_targets(failed_nodeids: Sequence[str]) -> tuple[str, ...]:
    """The files of the cached failures: what the failure cache guarantees.

    The fingerprint holds the set of test files, not the functions in them: a test removed or renamed by the fix
    would fail a node-id confirmation as not found on every run while the record lives. A whole file catches both
    the earlier failure and its neighbours."""
    return tuple(sorted({nodeid.partition("::")[0] for nodeid in failed_nodeids}))


@dataclass(slots=True)
class _Run:
    """One gate run: the profile, its boundaries and the state the report is built from."""

    options: RunOptions
    profile: Profile
    isolation: Isolation
    runtime: DiffRuntime
    environ: Environ
    state: RunState

    @property
    def root(self) -> Path:
        return self.profile.root

    def _timed[T](self, stage: str, action: Callable[[], T]) -> T:
        started = self.runtime.clock()
        try:
            return action()
        finally:
            self.state.timings[stage] += self.runtime.clock() - started

    def preflight(self) -> JsonObject | None:
        state = self.state
        try:
            self._timed("preflight", self.runtime.isolation_probe)
        except IsolationError as exc:
            state.error_detail = str(exc)
            sys.stderr.write(f"balamut diff: the isolation target is unreachable: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.PREFLIGHT,
                code="isolation_unreachable",
                next_action="fix_isolation_target_and_rerun",
            )
        return None

    def commands(self) -> JsonObject | None:
        state = self.state
        for command in self.profile.preflight:
            try:
                returncode = self._timed(
                    "preflight", lambda command=command: self.runtime.command_run(CommandInvocation(command))
                )
            except (OSError, subprocess.SubprocessError) as exc:
                state.error_detail = f"{shlex.join(command)}: {exc}"
                sys.stderr.write(f"balamut diff: preflight {state.error_detail}\n")
                return failure_report(
                    state,
                    RunStatus.INFRA_FAILURE,
                    stage=FailureStage.PREFLIGHT,
                    code="preflight_error",
                    next_action="fix_preflight_and_rerun",
                )
            if returncode != 0:
                state.error_detail = f"{shlex.join(command)} exited with {returncode}"
                return failure_report(
                    state,
                    RunStatus.INFRA_FAILURE,
                    stage=FailureStage.PREFLIGHT,
                    code="preflight_failed",
                    next_action="fix_preflight_and_rerun",
                )
        return None

    def mutation_invocation(
        self,
        *,
        scope_path: Path,
        stats_path: Path,
        raw_path: Path,
        refresh_failures_path: Path,
        fresh_generation: str | None,
    ) -> CommandInvocation:
        options = self.options
        assert self.state.scope is not None
        argv: tuple[str, ...] = (
            sys.executable,
            "-m",
            "balamut",
            "--root",
            str(self.root),
            "mutate",
            "--kind",
            ",".join(self.profile.diff_kinds),
            *options.mutate_args,
            "--address",
            default_address(self.profile),
            "--coverage",
            str(options.coverage_path),
            "--costs",
            str(substrate.costs_path(options.coverage_path)),
            "--diff",
            self.state.scope.diff_base_sha,
            "--diff-scope",
            str(scope_path),
            "--diff-mode",
            options.mode.value,
            "--stats-json",
            str(stats_path),
            "--refresh-failures-out",
            str(refresh_failures_path),
            "--json",
        )
        if fresh_generation is not None:
            # A substrate built by THIS run after pinning the scope has measured its unchanged files already.
            argv = (*argv, "--fresh-substrate-generation", fresh_generation)
        if options.agent_json:
            argv = (*argv, "--progress", "none")
        return CommandInvocation(argv=argv, stdout_path=raw_path)

    def run(self) -> JsonObject:
        state = self.state
        options = self.options
        if (failure := self.preflight()) is not None:
            return failure
        try:
            state.scope = self.runtime.scope_resolve(options.base, options.base_mode)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            sys.stderr.write(f"balamut diff: resolving the scope failed: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.SCOPE,
                code="scope_resolve_error",
                next_action="fix_git_environment_and_rerun",
            )
        sys.stderr.write(
            f"balamut diff: scope pinned ({len(state.scope.file_hashes)} files in the diff); an edit of any of them"
            " before the verdict stops the run as SCOPE_CHANGED\n"
        )
        options.raw_json_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=options.raw_json_path.parent, prefix=".balamut-diff.") as raw:
            return self._pinned(Path(raw), state.scope)

    def _pinned(self, temporary: Path, scope: diff_scope.DiffScope) -> JsonObject:
        state = self.state
        runtime = self.runtime
        scope_path = temporary / "scope.json"
        try:
            runtime.scope_write(scope, scope_path)
        except OSError as exc:
            sys.stderr.write(f"balamut diff: writing the scope failed: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.SCOPE,
                code="scope_write_error",
                next_action="fix_artifact_storage_and_rerun",
            )
        if (failure := self.commands()) is not None:
            return failure
        published = self._substrate(temporary, scope)
        if isinstance(published, dict):
            return published
        built_here, published_generation = published
        # A build takes minutes: an edit of the scope meanwhile would void the verdict after the campaign. The
        # published substrate stays usable: the next run's refresh re-measures what diverged.
        changes = runtime.scope_changes(scope, self.root)
        if not changes.current:
            return _scope_changed_report(state, changes)
        return self._mutation(temporary, scope, fresh_generation=published_generation if built_here else None)

    def _substrate(self, temporary: Path, scope: diff_scope.DiffScope) -> JsonObject | tuple[bool, str]:
        """Reuse, seed or build the substrate -> a failure report, or (built by this run, published generation)."""
        state = self.state
        options = self.options
        runtime = self.runtime
        layout = self.profile.layout
        expected = expected_metadata(self.profile, self.isolation)
        previous = runtime.substrate_inspect(options.coverage_path)
        state.substrate_miss_reason = substrate_miss_reason(
            options.coverage_path, previous, expected, force_rebuild=options.rebuild_substrate
        )
        if previous is not None:
            state.substrate_generation = previous.generation
        started = runtime.clock()
        fingerprint = baseline_failure.failure_fingerprint(
            expected,
            base_sha=scope.base_sha,
            diff_base_sha=scope.diff_base_sha,
            head_sha=scope.head_sha,
            environment=fingerprint_environment(self.profile, self.isolation, self.environ),
        )

        def done() -> None:
            state.timings["substrate"] = runtime.clock() - started

        cached = (
            None
            if options.rebuild_substrate
            else baseline_failure.failure_read(options.coverage_path, expected_fingerprint=fingerprint)
        )
        rebuild_after_confirmation = False
        if cached is not None:
            confirmation = runtime.measure(
                MeasureRequest(
                    profile=self.profile,
                    coverage_file=temporary / "baseline-confirmation.coverage",
                    targets=_confirmation_targets(cached.failed_nodeids),
                )
            )
            if confirmation.returncode != 0 and confirmation.failed_nodeids:
                done()
                state.baseline_failure_cached = True
                state.baseline_failure_tests = list(confirmation.failed_nodeids)
                baseline_failure.failure_write(
                    options.coverage_path,
                    baseline_failure.BaselineFailureRecord(fingerprint, confirmation.failed_nodeids, cached.generation),
                )
                return failure_report(
                    state,
                    RunStatus.BASELINE_FAILURE,
                    stage=FailureStage.SUBSTRATE,
                    code="baseline_failed",
                    next_action="fix_baseline_tests_and_rebuild_substrate",
                )
            if confirmation.returncode != 0:
                done()
                state.error_detail = confirmation.detail or None
                return failure_report(
                    state,
                    RunStatus.INFRA_FAILURE,
                    stage=FailureStage.SUBSTRATE,
                    code="baseline_confirmation_infra_failure",
                    next_action="fix_substrate_infrastructure_and_rerun",
                )
            baseline_failure.failure_remove(options.coverage_path)
            # A red build published the coverage of the other tests with the failed files unmeasured: the refresh
            # re-measures them. Without such a publication the substrate is built again.
            rebuild_after_confirmation = not (
                cached.generation and previous is not None and previous.generation == cached.generation
            )
            if rebuild_after_confirmation:
                state.substrate_miss_reason = "baseline_confirmation_passed"

        current_tree = substrate.tree_snapshot(layout)
        in_diff = frozenset(scope.file_hashes)
        drift = (
            substrate.tree_drift(
                options.coverage_path, previous.generation, current_tree, in_diff=in_diff, layout=layout
            )
            if previous is not None and state.substrate_miss_reason is None
            else None
        )
        drift_rebuild = drift is not None and not drift.refreshable(len(current_tree.hashes))
        if drift is not None and drift_rebuild:
            state.substrate_miss_reason = drift.reason
        # This tree's own substrate is unusable: the closest compatible one from the pool instead of a build from
        # scratch. A forced rebuild and a confirmed baseline failure need a build of this very tree.
        if state.substrate_miss_reason is not None and not options.rebuild_substrate and not rebuild_after_confirmation:
            seeded = runtime.substrate_seed(self.root, options.coverage_path, expected, current_tree, in_diff)
            if seeded is not None:
                previous = seeded
                drift = substrate.tree_drift(
                    options.coverage_path, seeded.generation, current_tree, in_diff=in_diff, layout=layout
                )
                drift_rebuild = drift is not None and not drift.refreshable(len(current_tree.hashes))
                state.substrate_seeded_from = None if drift_rebuild else seeded.generation
        self._announce_drift(drift, drift_rebuild=drift_rebuild)

        # Only this run's build is known to be measured after the scope was pinned; a generation a neighbour published
        # while this run waited for the lock may predate it. The tree is taken at the start of the build.
        trees_built_here: list[substrate.TreeRecord] = []

        def tree_measured() -> substrate.TreeRecord:
            tree = substrate.tree_snapshot(layout)
            trees_built_here.append(tree)
            return tree

        build_failed: list[str] = []
        estimate = build_estimate(substrate.costs_path(options.coverage_path), self.profile.coverage.workers_cap)

        def coverage_build(path: Path) -> int:
            sys.stderr.write(_build_banner(state.substrate_miss_reason, estimate))
            result = runtime.measure(MeasureRequest(profile=self.profile, coverage_file=path))
            build_failed[:] = result.failed_nodeids
            if result.returncode == 2 and result.detail:
                sys.stderr.write(f"balamut diff: the coverage build failed: {result.detail}\n")
            return result.returncode

        def red_test_files() -> frozenset[str]:
            return frozenset(_confirmation_targets(build_failed))

        mode = (
            SubstrateBuildMode.REBUILD
            if options.rebuild_substrate or rebuild_after_confirmation or drift_rebuild
            else SubstrateBuildMode.REUSE
        )
        try:
            published = runtime.substrate_ensure(
                options.coverage_path, expected, coverage_build, mode, tree_measured, red_test_files
            )
        except substrate.SubstrateBuildError as exc:
            done()
            sys.stderr.write(f"balamut diff: the baseline failed: {exc}\n")
            failed = tuple(build_failed)
            state.baseline_failure_tests = list(failed)
            if not failed:
                baseline_failure.failure_remove(options.coverage_path)
                return failure_report(
                    state,
                    RunStatus.INFRA_FAILURE,
                    stage=FailureStage.SUBSTRATE,
                    code="baseline_artifact_missing",
                    next_action="fix_substrate_infrastructure_and_rerun",
                )
            red_generation = exc.published.generation if isinstance(exc, substrate.SubstrateBaselineRedError) else ""
            if red_generation:
                state.substrate_generation = red_generation
            baseline_failure.failure_write(
                options.coverage_path, baseline_failure.BaselineFailureRecord(fingerprint, failed, red_generation)
            )
            return failure_report(
                state,
                RunStatus.BASELINE_FAILURE,
                stage=FailureStage.SUBSTRATE,
                code="baseline_failed",
                next_action="fix_baseline_tests_and_rebuild_substrate",
            )
        except (OSError, RuntimeError, substrate.SubstrateError, subprocess.SubprocessError) as exc:
            done()
            sys.stderr.write(f"balamut diff: the substrate failed: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.SUBSTRATE,
                code="substrate_error",
                next_action="fix_substrate_infrastructure_and_rerun",
            )
        done()
        state.substrate_generation = published.generation
        if trees_built_here:
            runtime.substrate_share(self.root, options.coverage_path, published)
        if previous is None or previous.generation != published.generation:
            # Another generation was published (a build here or a neighbour's): its own divergence is the refresh's.
            state.substrate_drift_files = 0
            state.substrate_seeded_from = None
        baseline_failure.failure_remove(options.coverage_path)
        state.substrate_reused = (
            previous is not None
            and previous.generation == published.generation
            and not options.rebuild_substrate
            and not rebuild_after_confirmation
            and not drift_rebuild
            and state.substrate_seeded_from is None
        )
        if state.substrate_reused:
            state.substrate_miss_reason = None
        elif state.substrate_miss_reason is None:
            state.substrate_miss_reason = "unexpected_rebuild"
        built_here = bool(trees_built_here) and not state.substrate_reused
        return built_here, published.generation

    def _announce_drift(self, drift: substrate.TreeDrift | None, *, drift_rebuild: bool) -> None:
        if drift is None:
            return
        if drift.new_src:
            sys.stderr.write(
                f"balamut diff: source files the build tree does not know, rebuilding: {_paths_brief(drift.new_src)}\n"
            )
            return
        if not (drift.paths or drift.support):
            return
        self.state.substrate_drift_files = 0 if drift_rebuild else len(drift.paths)
        action = "rebuilding" if drift_rebuild else "the refresh re-measures them"
        if drift.paths:
            sys.stderr.write(
                f"balamut diff: {len(drift.paths)} files outside the diff diverged from the build tree, {action}:"
                f" {_paths_brief(drift.paths)}\n"
            )
        if drift.support:
            sys.stderr.write(
                f"balamut diff: test support diverged from the build tree ({_paths_brief(drift.support)}),"
                f" executed by {len(drift.support_tests)} test files; {action}\n"
            )

    def _mutation(self, temporary: Path, scope: diff_scope.DiffScope, *, fresh_generation: str | None) -> JsonObject:
        state = self.state
        runtime = self.runtime
        raw_path = temporary / "mutation.json"
        stats_path = temporary / "mutation.stats.json"
        refresh_failures_path = temporary / "refresh.failures.json"
        invocation = self.mutation_invocation(
            scope_path=temporary / "scope.json",
            stats_path=stats_path,
            raw_path=raw_path,
            refresh_failures_path=refresh_failures_path,
            fresh_generation=fresh_generation,
        )
        started = runtime.clock()
        try:
            returncode = runtime.command_run(invocation)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            state.timings["mutation"] = runtime.clock() - started
            sys.stderr.write(f"balamut diff: the mutation boundary failed: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.MUTATION,
                code="mutation_command_error",
                next_action="fix_mutation_infrastructure_and_rerun",
            )
        state.timings["mutation"] = runtime.clock() - started
        if returncode == EXIT_SCOPE_CHANGED:
            # The engine checks the scope files during the campaign and stops at the first edit; the full check of
            # the orchestrator names the files and the moved refs.
            return _scope_changed_report(state, runtime.scope_changes(scope, self.root))
        if returncode == EXIT_REFRESH_BASELINE_FAILURE:
            # A test changed in the diff went red in the refresh: a baseline failure like a red build, but the
            # substrate is sound, so no failure record (confirming the fix would rebuild it whole).
            failed = failures.failed_nodeids_read(refresh_failures_path)
            if not failed:
                return failure_report(
                    state,
                    RunStatus.INFRA_FAILURE,
                    stage=FailureStage.COVERAGE_REFRESH,
                    code="refresh_failure_artifact_missing",
                    next_action="fix_mutation_infrastructure_and_rerun",
                )
            state.baseline_failure_tests = list(failed)
            return failure_report(
                state,
                RunStatus.BASELINE_FAILURE,
                stage=FailureStage.COVERAGE_REFRESH,
                code="baseline_failed",
                next_action="fix_baseline_tests_and_rerun",
            )
        if returncode != 0:
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.MUTATION,
                code="mutation_command_failed",
                next_action="fix_mutation_infrastructure_and_rerun",
            )
        changes = runtime.scope_changes(scope, self.root)
        if not changes.current:
            return _scope_changed_report(state, changes)
        state.scope_drift = list(changes.moved_refs)
        return self._gate(scope, raw_path, stats_path)

    def _gate(self, scope: diff_scope.DiffScope, raw_path: Path, stats_path: Path) -> JsonObject:
        state = self.state
        started = self.runtime.clock()
        try:
            mutants = diff_gate.parse_mutants(raw_path.read_text(encoding="utf-8"))
            stats = _mutation_stats(stats_path)
            _raw_validate(mutants, stats)
            state.evaluations = _scoped_evaluations(mutants, scope)
            result = diff_gate.gate(mutants, scope, root=self.root)
            findings = [_finding(mutant) for mutant in (*result.unresolved, *result.actionable, *result.uncovered)]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            state.timings["gate"] = self.runtime.clock() - started
            sys.stderr.write(f"balamut diff: the mutation artifacts are invalid: {exc}\n")
            return failure_report(
                state,
                RunStatus.INFRA_FAILURE,
                stage=FailureStage.GATE,
                code="artifact_invalid",
                next_action="fix_mutation_infrastructure_and_rerun",
            )
        state.timings["gate"] = self.runtime.clock() - started
        _stats_apply(state, stats)
        raw_path.replace(self.options.raw_json_path)
        if result.unresolved:
            return failure_report(
                state,
                RunStatus.UNRESOLVED,
                stage=FailureStage.GATE,
                code="unresolved_mutants",
                findings=findings,
                next_action="investigate_timeouts_and_rerun",
            )
        if result.actionable or result.uncovered:
            return failure_report(
                state,
                RunStatus.FINDINGS,
                stage=FailureStage.GATE,
                code="actionable_findings",
                findings=findings,
                next_action="fix_findings_and_rerun",
            )
        return report_build(state, RunStatus.CLEAN)


def status_exit_code(status: RunStatus) -> int:
    """A stable process exit code for each terminal status."""
    if status is RunStatus.CLEAN:
        return 0
    if status in {RunStatus.FINDINGS, RunStatus.UNRESOLVED}:
        return 1
    if status is RunStatus.SCOPE_CHANGED:
        return 3
    return 2


def _rendered_findings(findings: JsonValue) -> list[tuple[str, str, int, str, str]]:
    rendered: list[tuple[str, str, int, str, str]] = []
    if not isinstance(findings, list):
        return rendered
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        status, path, line, operator = (
            finding.get("status"),
            finding.get("path"),
            finding.get("line"),
            finding.get("operator"),
        )
        reason = finding.get("reason", "")
        if (
            isinstance(status, str)
            and isinstance(path, str)
            and isinstance(line, int)
            and isinstance(operator, str)
            and isinstance(reason, str)
        ):
            rendered.append((status, path, line, operator, reason))
    return rendered


def render_human(report: JsonMapping) -> str:
    lines = [
        f"balamut diff: {report['status']}",
        f"counts: {json.dumps(report['counts'], ensure_ascii=False, sort_keys=True)}",
    ]
    timings = report.get("timings")
    if isinstance(timings, dict):
        substrate_seconds = timings.get("substrate")
        mutation_seconds = timings.get("mutation")
        if isinstance(substrate_seconds, int | float) and isinstance(mutation_seconds, int | float):
            lines.append(
                f"time: substrate={substrate_seconds:.0f}s mutation={mutation_seconds:.0f}s"
                f" total={report_wall_seconds(report):.0f}s"
            )
    substrate_report = report.get("substrate")
    if isinstance(substrate_report, dict):
        miss_reason = substrate_report.get("miss_reason")
        if isinstance(miss_reason, str) and miss_reason:
            lines.append(f"substrate-miss: {miss_reason}")
    drift = report.get("scope_drift")
    scope_report = report.get("scope")
    if isinstance(drift, list) and drift and isinstance(scope_report, dict):
        moved = ", ".join(str(ref) for ref in drift)
        lines.append(
            f"scope-drift: {moved} moved during run;"
            f" verdict is for pinned diff base {scope_report.get('diff_base_sha')}"
        )
    baseline = report.get("baseline_failure")
    if isinstance(baseline, dict):
        tests = baseline.get("tests")
        if isinstance(tests, list) and tests:
            # Only the substrate stage writes the failure cache: a refresh failure leaves none, and a line about it
            # would promise a fast confirmation the next run will not make.
            if baseline.get("cached") is True:
                lines.append("baseline-cache: confirmed")
            elif report.get("failure_stage") == FailureStage.SUBSTRATE.value:
                lines.append("baseline-cache: recorded")
            named = [nodeid for nodeid in tests if isinstance(nodeid, str)]
            lines.extend(f"BASELINE {nodeid}" for nodeid in named[:10])
            if len(named) > 10:
                lines.append(f"BASELINE ... {len(named) - 10} more")
    rendered = _rendered_findings(report.get("findings"))
    # One line of code gives several findings (a deleted statement, `return None`, a constant): one gap in the tests,
    # shown as one line with all its operators.
    by_site: _SiteOutcomes = {}
    for status, path, line, operator, reason in sorted(rendered):
        by_site.setdefault((path, line), []).append((status, operator, reason))
    if rendered:
        # The status names ONE class (a timeout wins over findings); hundreds of survivors must not hide behind it.
        gating = Counter(status for status, *_rest in rendered)
        lines.append(
            f"gate: unresolved={gating['timeout']} actionable={gating['survived']} uncovered={gating['no_coverage']}"
            f" lines={len(by_site)}"
        )
    for (path, line), outcomes in by_site.items():
        status, _operator, reason = outcomes[0]
        if all((other, other_reason) == (status, reason) for other, _op, other_reason in outcomes):
            operators = ", ".join(operator for _status, operator, _reason in outcomes)
            suffix = f" {reason}" if reason else ""
            lines.append(f"{status.upper()} {path}:{line} [{operators}]{suffix}")
            continue
        detailed = ", ".join(
            f"{operator} ({other}{f': {other_reason}' if other_reason else ''})"
            for other, operator, other_reason in outcomes
        )
        lines.append(f"MIXED {path}:{line} [{detailed}]")
    error_detail = report.get("error_detail")
    if isinstance(error_detail, str) and error_detail:
        lines.append(f"detail: {error_detail}")
    next_action = report.get("next_action")
    if isinstance(next_action, str) and next_action:
        lines.append(f"next: {next_action}")
    return "\n".join(lines) + "\n"


def _parser(profile: Profile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="balamut diff",
        description="The diff mutation gate. Options it does not know are passed on to `balamut mutate`.",
    )
    parser.add_argument("--mode", choices=[mode.value for mode in DiffMode], default=DiffMode.FAST.value)
    parser.add_argument(
        "--base-mode",
        choices=[mode.value for mode in diff_scope.BaseMode],
        default=diff_scope.BaseMode.MERGE_BASE.value,
    )
    parser.add_argument(
        "--base", default=profile.base, help=f"the ref the diff is taken against (default {profile.base})"
    )
    parser.add_argument("--coverage", default=str(substrate_path(profile)))
    parser.add_argument("--json-out", default=str(profile.state_dir / "diff" / "mutation.json"))
    parser.add_argument("--report-json", default="", help="the final report (default: next to --json-out)")
    parser.add_argument("--agent-json", action="store_true", help="print the report as one JSON object")
    parser.add_argument("--rebuild-substrate", action="store_true", help="build the substrate from scratch")
    return parser


def _load(root: Path | None) -> Profile | int:
    try:
        return load_profile(root or Path.cwd())
    except ProfileError as exc:
        sys.stderr.write(f"error: invalid [tool.balamut]: {exc}\n")
        return 2


def main(
    argv: list[str], *, runtime: DiffRuntime | None = None, root: Path | None = None, environ: Environ | None = None
) -> int:
    loaded = _load(root)
    if isinstance(loaded, int):
        return loaded
    profile = loaded
    args, mutate_args = _parser(profile).parse_known_args(argv)
    resolved_environ: Environ = os.environ if environ is None else environ
    isolation = isolation_for(profile, environ=resolved_environ)
    dependencies = runtime or runtime_dependencies(profile, isolation)
    ensure_state_dir(profile)
    raw_json_path = Path(args.json_out)
    options = RunOptions(
        base=args.base,
        base_mode=diff_scope.BaseMode(args.base_mode),
        mode=DiffMode(args.mode),
        coverage_path=Path(args.coverage),
        raw_json_path=raw_json_path,
        report_json_path=Path(args.report_json) if args.report_json else raw_json_path.with_suffix(".report.json"),
        agent_json=args.agent_json,
        rebuild_substrate=args.rebuild_substrate,
        mutate_args=tuple(mutate_args),
    )
    state = RunState(invocation=_invocation_report(options))
    try:
        report = _Run(options, profile, isolation, dependencies, resolved_environ, state).run()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"balamut diff: the orchestration boundary failed: {exc}\n")
        report = failure_report(
            RunState(invocation=_invocation_report(options)),
            RunStatus.INFRA_FAILURE,
            stage=FailureStage.ORCHESTRATION,
            code="orchestration_error",
            next_action="fix_orchestration_infrastructure_and_rerun",
        )
    return _publish(report, options, dependencies)


def _publish(report: JsonObject, options: RunOptions, runtime: DiffRuntime) -> int:
    try:
        report_write(options.report_json_path, report)
    except OSError as exc:
        sys.stderr.write(f"balamut diff: publishing the report failed: {exc}\n")
        report = {
            **report,
            "status": RunStatus.INFRA_FAILURE.value,
            "findings": [],
            "evaluations": [],
            "failure_stage": FailureStage.REPORT,
            "error_code": "report_write_error",
            "error_detail": None,
            "next_action": "fix_report_storage_and_rerun",
        }
        try:
            report_write(options.report_json_path, report)
        except OSError as retry_exc:
            sys.stderr.write(f"balamut diff: publishing the report failed again: {retry_exc}\n")
            sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
            runtime.report_sink(report)
            return status_exit_code(RunStatus.INFRA_FAILURE)
    runtime.report_sink(report)
    if options.agent_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_human(report))
    return status_exit_code(RunStatus(str(report["status"])))


def _doctor(coverage_path: Path, *, profile: Profile, isolation: Isolation, runtime: DiffRuntime) -> JsonObject:
    issues: list[JsonValue] = []
    coverage_exists = coverage_path.is_file()
    metadata_exists = substrate.metadata_path(coverage_path).is_file()
    if not coverage_exists:
        issues.append("missing_coverage")
    if not metadata_exists:
        issues.append("missing_metadata")
    actual = runtime.substrate_inspect(coverage_path)
    if actual is None and coverage_exists and metadata_exists:
        issues.append(substrate_integrity_issue(coverage_path))
    elif actual is not None:
        issues.extend(substrate.metadata_mismatches(actual, expected_metadata(profile, isolation)))
    try:
        runtime.isolation_probe()
    except IsolationError:
        issues.append("isolation_unreachable")
    for command in profile.preflight:
        try:
            returncode = runtime.command_run(CommandInvocation(command))
        except OSError, subprocess.SubprocessError:
            returncode = 1
        if returncode != 0:
            issues.append("preflight_failed")
            break
    return {"status": "invalid" if issues else "valid", "issues": issues}


def doctor_main(
    argv: list[str], *, runtime: DiffRuntime | None = None, root: Path | None = None, environ: Environ | None = None
) -> int:
    """``balamut doctor``: check the substrate, the isolation target and the preflight without changing anything."""
    loaded = _load(root)
    if isinstance(loaded, int):
        return loaded
    profile = loaded
    parser = argparse.ArgumentParser(prog="balamut doctor", description="Read-only check of the gate's prerequisites.")
    parser.add_argument("--coverage", default=str(substrate_path(profile)))
    args = parser.parse_args(argv)
    isolation = isolation_for(profile, environ=os.environ if environ is None else environ)
    report = _doctor(
        Path(args.coverage),
        profile=profile,
        isolation=isolation,
        runtime=runtime or runtime_dependencies(profile, isolation),
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if report["status"] == "valid" else 2
