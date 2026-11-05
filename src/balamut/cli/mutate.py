"""``balamut mutate``: run a mutation campaign over paths, a diff or a pinned scope.

Exit codes: 0 for a completed campaign (survivors are findings, not a failure of the command), 2 for invalid
input or a setup failure, 4 when measuring coverage (a refresh or a substrate build) found failing tests, 5 when the
pinned scope changed while the campaign ran.

A direct campaign (paths, no diff) mutates the current tree: it builds the substrate when none fits and refreshes a
divergence into its store first, so a test written since the last campaign is a covering test of this one.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import cast

from balamut.cli.exit_codes import EXIT_REFRESH_BASELINE_FAILURE, EXIT_SCOPE_CHANGED, EXIT_USAGE
from balamut.cli.substrate_cmd import SubstrateBuildFailedError, ensure_substrate
from balamut.config import Layout, Profile, ProfileError, load_kind_overrides, load_profile
from balamut.core.mutants import ApplyMechanism
from balamut.core.triage import ColdKind, cold_kind
from balamut.coverage import costs as pytest_costs
from balamut.coverage import failures, substrate
from balamut.coverage.coverage_map import CoverageMap, load_coverage_map
from balamut.coverage.incremental import ingest_baseline, prepare_diff, refresh_tree
from balamut.coverage.measure import measure_coverage
from balamut.coverage.refresh import MeasureContext, RefreshBaselineError, refresh_tests
from balamut.coverage.store import Store, StoreCoverage, default_cache_path
from balamut.diff.scope import DiffScope, ScopeWatch, read_scope
from balamut.engine.campaign import CampaignOptions, run_campaign
from balamut.engine.collection import CollectionError, CollectionKey, build_collection_key, collect_test_nodeids
from balamut.engine.enumeration import (
    KindKinds,
    MutantCache,
    PathsByKind,
    UnknownKindError,
    enumerate_paths,
    filter_by_mechanism,
    filter_mutants_by_scope,
    known_kinds,
    mechanism_breakdown,
    resolve_kinds,
)
from balamut.engine.identify import oracle_collect_files
from balamut.engine.output import flat_report, read_invalidation_keys, result_json
from balamut.engine.partition import CoverageLike, SourceCache
from balamut.engine.progress import ProgressMode
from balamut.engine.results import CampaignScopeChangedError, CampaignSetupError, MutantResult
from balamut.engine.stats import CampaignTimings, empty_campaign_timings, empty_stats, write_campaign_stats
from balamut.engine.timeouts import timeout_policy_error
from balamut.gitutil import git_output_in
from balamut.isolation import EnvMap, Isolation, isolation_for
from balamut.operators.facts import SelfCovered
from balamut.paths import campaign_scratch, default_address, ensure_state_dir, substrate_path
from balamut.report.agent import (
    CountMap,
    MutantRecord,
    build_report,
    records_from_store,
    render_agent_report,
    split_cold_records,
)
from balamut.report.html import ReportSummary, RunInfo, read_baseline, render_html, write_html
from balamut.runtime.inmemory import code_reachable
from balamut.runtime.plugin import TEST_ROOTS_ENV

_PLUGIN = "balamut.runtime.plugin"
# The process environment a campaign shares with its daemons (pytest-fast fingerprints some of it).
CAMPAIGN_ENV = (
    "PYTEST_ADDOPTS",
    "PYTEST_FAST_MARK",
    "PYTEST_FAST_ENV_PREFIXES",
    "PYTEST_FAST_WATCH_DIRS",
    TEST_ROOTS_ENV,
)


def _parser(profile: Profile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="balamut mutate", description="Mutation testing on a warm pytest-fast pool.")
    parser.add_argument("paths", nargs="*", help="source files or directories (empty with --diff)")
    parser.add_argument("--coverage", default=str(substrate_path(profile)), help="the per-test coverage substrate")
    parser.add_argument("--costs", default="", help="the substrate's cost sidecar; empty: calibrate on demand")
    parser.add_argument("--address", default=default_address(profile), help="socket path prefix of the daemons")
    parser.add_argument("--workers", type=int, default=1, help="workers per daemon (default 1)")
    parser.add_argument("--daemons", type=int, default=0, help="parallel daemons (default: planned from the work)")
    parser.add_argument("--mutant-timeout", type=float, default=profile.mutant_timeout, help="no-progress timeout, s")
    parser.add_argument("--timeout-retries", type=int, default=profile.timeout_retries, help="isolated retries")
    parser.add_argument("--timeout-retry-multiplier", type=float, default=3.0, help="timeout growth per retry")
    parser.add_argument("--progress", choices=[mode.value for mode in ProgressMode], default=ProgressMode.AUTO.value)
    parser.add_argument("--diff", default="", help="mutate source changed against BASE")
    parser.add_argument("--diff-scope", default="", help="a pinned JSON scope of changed files and lines")
    parser.add_argument(
        "--diff-mode",
        choices=("fast", "full"),
        default="full",
        help="fast mutates only the changed site spans; full every mutant of the changed files",
    )
    parser.add_argument(
        "--fresh-substrate-generation",
        default=None,
        help="a substrate generation built after pinning --diff-scope: its unchanged files need no refresh",
    )
    parser.add_argument("--refresh-failures-out", default="", help="where to write node ids failing in the refresh")
    parser.add_argument("--invalidate-sites-from", default="", help="drop the keys of a raw JSON or a report")
    parser.add_argument("--refresh-tests", default="", help="confirm loop: re-measure these test files first")
    parser.add_argument(
        "--refresh-substrate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="a direct campaign first brings the substrate up to the current tree (--no-refresh-substrate: as is)",
    )
    parser.add_argument("--cache", default="", help="the verdict cache (default: per branch and worktree)")
    parser.add_argument("--no-cache", action="store_true", help="do not read cached verdicts (they are still written)")
    parser.add_argument(
        "--schedule-fuzz",
        action=argparse.BooleanOptionalAction,
        default=profile.schedule_fuzz,
        help="inject yield points into concurrency mutants",
    )
    parser.add_argument("--cross-kill", action="store_true", help="run every covering test per mutant (kill matrix)")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of mutants (smoke runs)")
    parser.add_argument("--kind", default=",".join(profile.kinds), help="comma-separated mutant kinds")
    parser.add_argument("--operator", default="", help="keep only operators with this prefix")
    parser.add_argument(
        "--mechanism",
        default="",
        help=f"comma-separated apply mechanisms ({','.join(m.value for m in ApplyMechanism)})",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print the raw results as a JSON array")
    output.add_argument("--agent-json", action="store_true", help="print the clustered agent report as JSON")
    output.add_argument("--flat", action="store_true", help="print survivors by bucket instead of clusters")
    parser.add_argument("--html", default="", help="also write a self-contained HTML report to this path")
    parser.add_argument(
        "--html-baseline",
        default="",
        help="an earlier HTML report: --html compares against it and carries its score history forward",
    )
    parser.add_argument("--detailed", action="store_true", help="a per-mutant cost breakdown on stderr and JSON")
    parser.add_argument("--detailed-json", default="", help="where --detailed writes its JSON")
    parser.add_argument("--stats-json", default="", help="an atomic JSON snapshot of the campaign's progress")
    return parser


def _error(message: str) -> int:
    sys.stderr.write(f"error: {message}\n")
    return EXIT_USAGE


@dataclass(frozen=True, slots=True)
class HtmlRequest:
    """Where ``--html`` writes and the earlier report it compares against, both checked before the campaign runs."""

    path: Path
    baseline: ReportSummary | None


def _html_request(args: argparse.Namespace) -> HtmlRequest | None:
    """The validated ``--html`` options; a ValueError names the problem. The baseline may be the output path
    itself: it is read here, before the new report replaces it."""
    if args.html_baseline and not args.html:
        raise ValueError("--html-baseline needs --html")
    if not args.html:
        return None
    path = Path(args.html)
    if not path.parent.is_dir():
        raise ValueError(f"the directory of --html does not exist: {path.parent}")
    if not args.html_baseline:
        return HtmlRequest(path, None)
    try:
        baseline = read_baseline(Path(args.html_baseline))
    except ValueError as exc:
        raise ValueError(f"invalid --html-baseline: {exc}") from exc
    return HtmlRequest(path, baseline)


def _scope_label(args: argparse.Namespace, scope: DiffScope | None) -> tuple[str, ...]:
    if scope is not None:
        return (f"diff {scope.diff_base_sha[:12]}..{scope.head_sha[:12]}",)
    if args.diff:
        return (f"diff against {args.diff}",)
    return tuple(args.paths)


def _write_html_report(
    request: HtmlRequest,
    args: argparse.Namespace,
    *,
    results: list[MutantResult],
    sources: SourceCache,
    profile: Profile,
    kinds: list[str],
    scope: DiffScope | None,
    started: float,
) -> None:
    """Write the HTML report; as with the printed report, a failure here never fails the campaign."""
    root = profile.root
    branch = git_output_in(root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    info = RunInfo(
        project=root.name,
        generated_at=datetime.now().astimezone(),
        scope=_scope_label(args, scope),
        kinds=tuple(kinds),
        commit=git_output_in(root, "rev-parse", "--short=12", "HEAD") or "",
        branch="" if branch == "HEAD" else branch,  # a detached HEAD has no branch to name
        duration_s=time.perf_counter() - started,
    )
    try:
        page = render_html(results, sources, info=info, semantic=profile.operators.semantic, baseline=request.baseline)
        write_html(request.path, page)
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(
            f"writing the HTML report failed ({exc!r}); the verdicts are cached: run again for cache hits\n"
        )
        return
    sys.stderr.write(f"html report: {request.path}\n")


def campaign_environment(profile: Profile, isolation: Isolation) -> EnvMap:
    """The process environment shared by the client and its daemons.

    The plugin is loaded into every daemon; the project's marker filter is cleared (a mutant is judged by every
    covering test); the source and test roots drive pytest-fast's staleness scan. With isolated slots the daemons'
    environments differ by design, so no application prefix is fingerprinted."""
    addopts = os.environ.get("PYTEST_ADDOPTS", "")
    if _PLUGIN not in addopts:
        addopts = f"{addopts} -p {_PLUGIN}".strip()
    layout = profile.layout
    return {
        "PYTEST_ADDOPTS": addopts,
        "PYTEST_FAST_MARK": os.environ.get("PYTEST_FAST_MARK", ""),
        "PYTEST_FAST_ENV_PREFIXES": "" if isolation.isolated else ",".join(profile.env_prefixes),
        "PYTEST_FAST_WATCH_DIRS": ",".join(layout.watch_dirs()),
        TEST_ROOTS_ENV: ",".join(layout.tests),
    }


def _cold_records(
    records: list[MutantRecord], results: list[MutantResult], sources: SourceCache
) -> tuple[list[MutantRecord], CountMap]:
    """Separate the cold records of this campaign, with the exact mutant span where it is known."""
    by_key = {result.key: result for result in results if result.key}

    def reachable(file: str, lineno: int) -> bool:
        source = sources.get(file)
        return code_reachable(source, lineno) if source is not None else True

    def resolve(record: MutantRecord) -> ColdKind:
        source = sources.get(record["file"])
        result = by_key.get(record.get("key", ""))
        if result is None:
            return cold_kind(source, line=record["lineno"])
        m = result.mutant
        return cold_kind(source, line=m.site_lineno, start_byte=m.start_byte, end_byte=m.end_byte)

    return split_cold_records(records, reachable, resolve)


def _managed_substrate(cov_path: Path) -> bool:
    """A substrate balamut maintains: missing (it is built) or published with metadata. A bare coverage file passed
    with ``--coverage`` records neither how nor on which tree it was measured: it is read as is."""
    return not cov_path.exists() or substrate.metadata_path(cov_path).exists()


def _refresh_substrate(
    args: argparse.Namespace,
    *,
    profile: Profile,
    store: Store,
    context: MeasureContext,
    cov_path: Path,
    timings: CampaignTimings,
) -> int | None:
    """Build the substrate when none fits the tree, refresh the rest of the divergence -> an exit code on failure."""
    measure = partial(measure_coverage, isolation=context.isolation, launcher=context.launcher)
    try:
        started = time.perf_counter()
        ensured = ensure_substrate(profile, context.isolation, cov_path, measure=measure, refresh_drift=True)
        if ensured.built_reason is not None:
            timings["coverage_refresh"] += time.perf_counter() - started
            sys.stderr.write(
                f"substrate: built generation {ensured.metadata.generation[:12]} (reason: {ensured.built_reason})\n"
            )
        refresh_tree(cov_path=cov_path, store=store, context=context, phase_timings=timings)
    except (SubstrateBuildFailedError, RefreshBaselineError) as failure:
        sys.stderr.write(f"error: {failure}\n")
        for nodeid in failure.nodeids:
            sys.stderr.write(f"BASELINE {nodeid}\n")
        if not failure.nodeids:
            return EXIT_USAGE
        if args.refresh_failures_out:
            failures.failed_nodeids_write(Path(args.refresh_failures_out), failure.nodeids)
        return EXIT_REFRESH_BASELINE_FAILURE
    except RuntimeError as exc:
        return _error(f"the substrate refresh failed: {exc}")
    return None


def _warn_stale_substrate(cov_path: Path, layout: Layout) -> None:
    """A campaign reading the substrate as is: say so when the source or test files changed since it was measured."""
    metadata = substrate.substrate_inspect(cov_path)
    recorded = substrate.tree_read(cov_path, metadata.generation) if metadata is not None else None
    if recorded is None:
        return
    diverged = sorted(substrate.tree_diverged(recorded, substrate.tree_snapshot(layout)))
    if diverged:
        shown = ", ".join(diverged[:5]) + (f" (+{len(diverged) - 5} more)" if len(diverged) > 5 else "")
        sys.stderr.write(
            f"warning: the substrate was measured on another version of {shown}; run `balamut substrate`"
            " (covering tests may be stale)\n"
        )


def _write_empty_stats(path: Path | None, timings: CampaignTimings) -> None:
    if path is not None:
        write_campaign_stats(path, empty_stats(complete=True, timings=timings))


def _routing(paths: list[str], kinds: list[str], layout: Layout, self_covered_kinds: frozenset[str]) -> PathsByKind:
    source = frozenset(path for path in paths if layout.is_source_path(path))
    tests = frozenset(path for path in paths if layout.is_test_path(path))
    return {kind: tests if kind in self_covered_kinds else source for kind in kinds}


def _enum_cache(profile: Profile, resolved: KindKinds) -> MutantCache:
    """The content-addressed enumeration cache, keyed by the campaign's kind configuration."""
    # The facts are frozen dataclasses at runtime, but the kind registry types them as the capability base.
    canonical = _canonical(cast("Canonical", resolved))
    config_hash = hashlib.sha256(repr(canonical).encode()).hexdigest()[:16]
    return MutantCache(profile.state_dir / "tmp" / "enum" / config_hash)


type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type Canonical = JsonValue | frozenset[Canonical] | tuple[Canonical, ...] | list[Canonical] | dict[str, Canonical]


def _canonical(value: Canonical) -> Canonical:
    """A hash-seed-stable serialization of a resolved kind registry: frozensets and dicts sort, dataclasses
    recurse through their fields, so two processes always fingerprint the same configuration identically."""
    if isinstance(value, frozenset):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, dict):
        return sorted(((key, _canonical(item)) for key, item in value.items()), key=repr)
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [type(value).__qualname__, _canonical(dataclasses.asdict(value))]
    return value


def main(argv: list[str]) -> int:
    try:
        profile = load_profile(Path.cwd())
    except ProfileError as exc:
        return _error(f"invalid [tool.balamut]: {exc}")
    started = time.perf_counter()
    args = _parser(profile).parse_args(argv)
    layout = profile.layout
    timings = empty_campaign_timings()
    try:
        html_report = _html_request(args)
    except ValueError as exc:
        return _error(str(exc))

    stats_path = Path(args.stats_json) if args.stats_json else None
    if stats_path is not None:
        if not stats_path.parent.is_dir():
            return _error(f"the directory of --stats-json does not exist: {stats_path.parent}")
        stats_path.unlink(missing_ok=True)
    if (
        problem := timeout_policy_error(args.mutant_timeout, args.timeout_retry_multiplier, args.timeout_retries)
    ) is not None:
        return _error(problem)
    if args.diff_mode == "fast" and not args.diff_scope:
        return _error("--diff-mode fast needs a pinned --diff-scope")
    scope: DiffScope | None = None
    if args.diff_scope:
        try:
            scope = read_scope(Path(args.diff_scope))
        except (OSError, UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
            return _error(f"invalid --diff-scope {args.diff_scope}: {exc}")
    scope_watch = ScopeWatch(scope, repo_root=layout.root) if scope is not None else None
    invalidation: set[str] = set()
    if args.invalidate_sites_from:
        try:
            invalidation = read_invalidation_keys(Path(args.invalidate_sites_from))
        except ValueError as exc:
            return _error(f"invalid --invalidate-sites-from: {exc}")
    kinds = [kind.strip() for kind in args.kind.split(",") if kind.strip()]
    registered = load_kind_overrides(profile.root)
    available = known_kinds() | set(registered.kinds) - registered.removed
    if unknown_kinds := sorted(set(kinds) - available):
        return _error(f"unknown --kind {unknown_kinds}; known: {','.join(sorted(available))}")
    mechanisms = frozenset(value.strip() for value in args.mechanism.split(",") if value.strip())
    known_mechanisms = {mechanism.value for mechanism in ApplyMechanism}
    if unknown := sorted(mechanisms - known_mechanisms):
        return _error(f"unknown --mechanism {unknown}; known: {','.join(sorted(known_mechanisms))}")
    chosen = frozenset(ApplyMechanism(value) for value in mechanisms)

    isolation = isolation_for(profile)
    ensure_state_dir(profile)
    os.environ.update(campaign_environment(profile, isolation))
    context = MeasureContext(profile=profile, isolation=isolation)
    cov_path = Path(args.coverage)
    cache_path = Path(args.cache) if args.cache else default_cache_path(profile.root, profile.state_dir)
    sys.stderr.write(f"cache: {cache_path}\n")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cache_path)
    try:
        return _run(
            args,
            profile=profile,
            store=store,
            kinds=kinds,
            chosen=chosen,
            scope=scope,
            scope_watch=scope_watch,
            invalidation=invalidation,
            isolation=isolation,
            context=context,
            cov_path=cov_path,
            stats_path=stats_path,
            timings=timings,
            html_report=html_report,
            started=started,
        )
    finally:
        store.close()


def _run(
    args: argparse.Namespace,
    *,
    profile: Profile,
    store: Store,
    kinds: list[str],
    chosen: frozenset[ApplyMechanism],
    scope: DiffScope | None,
    scope_watch: ScopeWatch | None,
    invalidation: set[str],
    isolation: Isolation,
    context: MeasureContext,
    cov_path: Path,
    stats_path: Path | None,
    timings: CampaignTimings,
    html_report: HtmlRequest | None,
    started: float,
) -> int:
    layout = profile.layout
    if invalidation:
        deleted = store.delete_mutants(invalidation)
        sys.stderr.write(f"cache invalidation: deleted {deleted} of {len(invalidation)} keys\n")
    sys.stderr.write(f"baseline cached: {store.count_mutants()}\n")
    refresh_list = [test.strip() for test in args.refresh_tests.split(",") if test.strip()]
    try:
        resolved = resolve_kinds(kinds, load_kind_overrides(profile.root))
    except UnknownKindError as exc:
        return _error(str(exc))
    kind_kinds = resolved
    self_covered_kinds = frozenset(
        kind for kind, facts in kind_kinds.items() if any(isinstance(fact, SelfCovered) for fact in facts)
    )
    # A pure self-covered run never reads the coverage file: its mutants are covered by their own tests. A
    # coverage file is needed only when coverage comes from it: a direct run, or the first ingest into an
    # empty store.
    pure_oracle = bool(kinds) and all(kind in self_covered_kinds for kind in kinds)
    needs_coverage = not pure_oracle and (not (args.diff or refresh_list) or not store.has_coverage())
    direct_refresh = (
        needs_coverage
        and bool(args.paths)
        and not (args.diff or refresh_list)
        and args.refresh_substrate
        and _managed_substrate(cov_path)
    )
    if needs_coverage and not direct_refresh and not cov_path.exists():
        return _error(f"no coverage substrate at {cov_path}: build it first (balamut substrate does)")
    paths_by_kind: PathsByKind | None = None
    cov: CoverageLike
    if args.diff:
        try:
            paths = prepare_diff(
                args.diff,
                cov_path=cov_path,
                store=store,
                context=context,
                scope=scope,
                include_tests=bool(self_covered_kinds),
                phase_timings=timings,
                fresh_generation=args.fresh_substrate_generation,
            )
        except RefreshBaselineError as failure:
            # A changed test went red in the refresh: a baseline failure with test names, not an engine failure.
            sys.stderr.write(f"error: {failure}\n")
            if args.refresh_failures_out:
                failures.failed_nodeids_write(Path(args.refresh_failures_out), failure.nodeids)
            return EXIT_REFRESH_BASELINE_FAILURE
        # A refresh takes minutes: an edit of the scope meanwhile would void both it and the campaign.
        if scope_watch is not None and (changed := scope_watch.changed(force=True)):
            sys.stderr.write(f"error: the scope changed during the refresh: {', '.join(changed)}\n")
            return EXIT_SCOPE_CHANGED
        paths_by_kind = _routing(paths, kinds, layout, self_covered_kinds)
        cov = StoreCoverage(store)
    elif refresh_list:
        if not store.has_coverage():
            ingest_baseline(cov_path, store, root=layout.root)
        started = time.perf_counter()
        measured = refresh_tests(refresh_list, store=store, context=context)
        timings["coverage_refresh"] += time.perf_counter() - started
        sys.stderr.write(f"refresh tests: {len(refresh_list)} file(s), {measured} tests re-measured\n")
        paths = list(args.paths)
        cov = StoreCoverage(store)
    elif direct_refresh:
        if (
            failure := _refresh_substrate(
                args, profile=profile, store=store, context=context, cov_path=cov_path, timings=timings
            )
        ) is not None:
            return failure
        paths = list(args.paths)
        cov = StoreCoverage(store)
    else:
        paths = list(args.paths)
        cov = load_coverage_map(cov_path, root=layout.root) if cov_path.exists() else CoverageMap(by_file={})
        _warn_stale_substrate(cov_path, layout)
    if not paths:
        sys.stderr.write("nothing to mutate (an empty diff or no paths)\n")
        _write_empty_stats(stats_path, timings)
        if args.json and stats_path is not None:
            sys.stdout.write("[]\n")
        if html_report is not None:
            _write_html_report(
                html_report, args, results=[], sources={}, profile=profile, kinds=kinds, scope=scope, started=started
            )
        return 0

    mutants, sources, reexport = enumerate_paths(
        paths,
        kinds,
        layout=profile.layout,
        resolved=resolved,
        paths_by_kind=paths_by_kind,
        phase_timings=timings,
        enum_cache=_enum_cache(profile, resolved),
    )
    if args.diff_mode == "fast" and scope is not None:
        mutants = filter_mutants_by_scope(mutants, scope)
    cold = mechanism_breakdown(mutants, sources).get(ApplyMechanism.COLD.value, 0)
    if cold:
        sys.stderr.write(f"mechanisms: {len(mutants) - cold} code / {cold} cold (module or class level)\n")
    if args.operator:
        mutants = [mutant for mutant in mutants if mutant.operator.startswith(args.operator)]
    if chosen:
        mutants = filter_by_mechanism(mutants, sources, chosen)
    if args.limit:
        mutants = mutants[: args.limit]
    requested = str(args.daemons) if args.daemons > 0 else "auto"
    sys.stderr.write(f"mutants: {len(mutants)}, daemons: {requested} x {args.workers} workers\n")

    # Oracle covering tests expand into the instances pytest actually collects (parametrized ids), collected once
    # under the files that collect them (a name-matched test file or the wrapper of a re-exported test).
    oracle_files = oracle_collect_files(mutants, reexport)
    collection_key: CollectionKey | None = None
    if oracle_files and scope is not None:
        metadata = substrate.substrate_inspect(cov_path)
        if metadata is None:
            return _error("an oracle collection over a scope needs a valid coverage substrate")
        collection_key = build_collection_key(
            oracle_files, scope=scope, metadata=metadata, reexport=reexport, root=layout.root
        )
    try:
        started = time.perf_counter()
        collected = collect_test_nodeids(oracle_files, store=store, cache_key=collection_key, root=layout.root)
        timings["pytest_collection"] += time.perf_counter() - started
    except CollectionError as error:
        return _error(f"the oracle collection failed: {error}")

    detailed_path: Path | None = None
    if args.detailed:
        detailed_path = Path(args.detailed_json) if args.detailed_json else campaign_scratch(profile) / "detailed.json"
        detailed_path.parent.mkdir(parents=True, exist_ok=True)
    options = CampaignOptions(
        address_base=args.address,
        layout=layout,
        scratch=campaign_scratch(profile),
        isolation=isolation,
        daemons=args.daemons,
        workers=args.workers,
        schedule_fuzz=args.schedule_fuzz,
        cross_kill=args.cross_kill,
        mutant_timeout=args.mutant_timeout,
        timeout_retries=args.timeout_retries,
        timeout_retry_multiplier=args.timeout_retry_multiplier,
        daemon_ttl=profile.daemon_ttl,
        progress_mode=ProgressMode(args.progress),
        stats_path=stats_path,
        detailed=args.detailed,
        detailed_path=detailed_path,
        semantic=profile.operators.semantic,
    )
    # In diff mode the sidecar already seeded the cost store at the ingest of its generation: laying it over the store
    # would overwrite durations refreshed since then with build-time ones.
    initial_costs = pytest_costs.read_costs(Path(args.costs)) if args.costs and not args.diff else None
    try:
        results = run_campaign(
            mutants,
            sources,
            cov,
            options=options,
            store=store,
            read_cache=not args.no_cache,
            reexport=reexport,
            collected_test_nodeids=collected,
            phase_timings=timings,
            initial_costs=initial_costs,
            scope_watch=scope_watch,
        )
    except CampaignScopeChangedError as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_SCOPE_CHANGED
    except (CampaignSetupError, CollectionError) as error:
        return _error(str(error))
    code = _emit(args, results, store=store, sources=sources, profile=profile)
    if html_report is not None:
        _write_html_report(
            html_report,
            args,
            results=results,
            sources=sources,
            profile=profile,
            kinds=kinds,
            scope=scope,
            started=started,
        )
    return code


def _emit(
    args: argparse.Namespace, results: list[MutantResult], *, store: Store, sources: SourceCache, profile: Profile
) -> int:
    """Print the results; a rendering failure never fails the run (the verdicts are cached already)."""
    needs_agent = args.agent_json or (not args.json and not args.flat)
    # Only keys of this campaign: the store also holds stale rows of earlier versions of the same files.
    current = {result.key for result in results if result.key}
    records = records_from_store(store, sorted(sources), current) if needs_agent else []
    cold_counts: CountMap = {}
    if needs_agent:
        records, cold_counts = _cold_records(records, results, sources)
    counts = Counter(result.status.value for result in results)
    sys.stderr.write(
        f"campaign final: done={len(results)} killed={counts.get('killed', 0)} survived={counts.get('survived', 0)}"
        f" timeout={counts.get('timeout', 0)} no_coverage={counts.get('no_coverage', 0)}\n"
    )
    try:
        if args.agent_json:
            report = build_report(records, cold_counts=cold_counts)
            sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        elif args.json:
            rows = [result_json(result) for result in results]
            sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        elif args.flat:
            flat_report(results, semantic=profile.operators.semantic)
        else:
            sys.stdout.write(render_agent_report(build_report(records, cold_counts=cold_counts)))
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(
            f"\nprinting the results failed ({exc!r}); the verdicts are cached: run again for cache hits\n"
        )
    return 0
