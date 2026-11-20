"""One worker thread per daemon: take mutants until the queue is empty and decide each verdict exactly.

A candidate kill is accepted only for killers that pass on the immediately restored original (the paired run); a
pytest worker whose paired run fails is replaced before the next mutant. A warm verdict whose run never entered the
mutated code object is deferred to a confirmation in a fresh fork. A hang is a kill only when it is causal. Any
daemon failure requeues the mutant and ends this worker; a failure to prove exactness ends the lane.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from balamut.config import DEFAULT_SEMANTIC
from balamut.core.semantic import SemanticPatterns
from balamut.coverage.store import CostMap, Store
from balamut.diff.scope import ScopeWatch
from balamut.engine.daemon import (
    Daemon,
    Hang,
    RunResult,
    daemon_process_failure_nodeid,
    order_covering,
    process_failure_suffix,
    respawn_daemon,
    retire_persistent_pool,
    run_subset_guarded,
    write_original_spec,
    write_request_spec,
)
from balamut.engine.partition import KillRecord, ResultsByIndex, Work, persist_verdict
from balamut.engine.progress import CampaignProgress
from balamut.engine.results import (
    CampaignExactnessError,
    CampaignScopeChangedError,
    DaemonStaleError,
    MutantResult,
    MutantStatus,
    VerdictReason,
)
from balamut.engine.timeouts import mutant_timeout_budget
from balamut.runtime.kill_deps import KILL_DEPS_SUFFIX, KillDeps, KillValidity, kill_deps_read
from balamut.runtime.plugin import STALE_MARKER_SUFFIX, TOUCHED_MARKER_SUFFIX, UNSUPPORTED_MARKER_SUFFIX

type KillersByKey = dict[str, frozenset[str]]  # mutant key -> node ids of its earlier killers

_EMPTY: frozenset[str] = frozenset()


@dataclass(slots=True)
class Timing:
    write: float = 0.0
    request: float = 0.0
    n: int = 0
    costs: CostMap = field(default_factory=dict[str, float])  # freshly measured durations for the cost store
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, write: float, request: float, costs: CostMap | None = None) -> None:
        with self.lock:
            self.write += write
            self.request += request
            self.n += 1
            if costs:
                self.costs.update(costs)


@dataclass(slots=True)
class MutantDiag:
    """The cost breakdown of one mutant (``--detailed``): request = spec write + fork + patch + the setup, call and
    teardown of the tests it ran + the bus; the overhead is the request minus the observed test work."""

    key: str
    path: str
    lineno: int
    operator: str
    status: str
    covering: int
    tests_run: int  # tests actually run before the first kill (killer-first short-circuit)
    setup_s: float  # fixture cost, which persistent workers amortize
    call_s: float  # test bodies and database work, which nothing amortizes
    teardown_s: float
    request_s: float
    write_s: float
    killer: str


@dataclass(slots=True)
class Diagnostics:
    """A thread-safe collector of per-mutant diagnostics."""

    rows: list[MutantDiag] = field(default_factory=list[MutantDiag])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, row: MutantDiag) -> None:
        with self.lock:
            self.rows.append(row)


def _note(message: str) -> None:
    sys.stderr.write(f"\n  {message}\n")
    sys.stderr.flush()


def hang_verdict(
    daemon: Daemon, hang: Hang, *, touched: bool, costs: CostMap, timeout: float, label: str
) -> tuple[str | None, VerdictReason]:
    """A final hang is a kill when it is causal: a suite with such a mutant would not pass.

    Causality is three facts: the mutated code object executed (the touch marker), the hung covering test is known,
    and the same test passes on the original in the same budget. The daemon was already respawned on the original.
    -> (killer or None, reason)."""
    hung = hang.hung_nodeid()
    if not touched or hung is None:
        _note(f"timeout {label}: a hang without proven causality (touched={touched}, test={hung or '?'}); unresolved")
        return None, VerdictReason.HANG_UNCONFIRMED
    daemon.mutant_file.write_text("", encoding="utf-8")
    original = run_subset_guarded(
        daemon, [hung], costs, full=True, timeout=timeout, label=f"{label} [original-hang]", fresh_workers=True
    )
    if isinstance(original, Hang):
        respawn_daemon(daemon)
        _note(f"timeout {label}: {hung} hangs on the original too; unresolved")
        return None, VerdictReason.ORIGINAL_HANG
    if original.rc != 0:
        _note(f"timeout {label}: {hung} fails on the original; unresolved")
        return None, VerdictReason.ORIGINAL_FAILED
    _note(f"hang-kill {label}: {hung} hangs on the mutant and passes on the original")
    return hung, VerdictReason.HANG


def requeue_after_failure(daemon: Daemon, item: Work, exc: Exception, pending: queue.Queue[Work]) -> None:
    """The fault-isolation boundary: the daemon died or its socket dropped. A computed verdict is already cached;
    the mutant goes back to the queue for a live daemon and this worker ends (bounded: one failure per worker)."""
    _note(f"worker {daemon.address} failed on {item.mutant.path}:{item.mutant.lineno}: {exc!r}; requeued, exiting")
    pending.put(item)


def campaign_scope_abort(fatal_errors: queue.Queue[CampaignExactnessError] | None, changed: tuple[str, ...]) -> None:
    """Tell the lane the scope changed: it raises once every worker stopped at a mutant boundary."""
    error = CampaignScopeChangedError(changed)
    if fatal_errors is None:
        raise error
    fatal_errors.put(error)


@dataclass(frozen=True, slots=True)
class _Markers:
    unsupported: Path
    touched: Path
    stale: Path
    deps: Path

    @classmethod
    def of(cls, daemon: Daemon) -> _Markers:
        return cls(
            Path(f"{daemon.mutant_file}{UNSUPPORTED_MARKER_SUFFIX}"),
            Path(f"{daemon.mutant_file}{TOUCHED_MARKER_SUFFIX}"),
            Path(f"{daemon.mutant_file}{STALE_MARKER_SUFFIX}"),
            Path(f"{daemon.mutant_file}{KILL_DEPS_SUFFIX}"),
        )

    def clear(self) -> None:
        for marker in (self.unsupported, self.touched, self.stale, self.deps):
            marker.unlink(missing_ok=True)


def worker_loop(
    daemon: Daemon,
    pending: queue.Queue[Work],
    results: ResultsByIndex,
    progress: CampaignProgress,
    timing: Timing,
    costs: CostMap,
    store: Store | None,
    *,
    cross_kill: bool,
    mutant_timeout: float,
    finalize_timeout: bool,
    detailed: bool = False,
    diags: Diagnostics | None = None,
    killers_by_key: KillersByKey | None = None,
    defer_untouched: queue.Queue[Work] | None = None,
    reject_untouched: bool = False,
    reject_unstable_kill: bool = False,
    fresh_workers: bool = False,
    fatal_errors: queue.Queue[CampaignExactnessError] | None = None,
    kill_validity: KillValidity | None = None,
    scope_watch: ScopeWatch | None = None,
    semantic: SemanticPatterns = DEFAULT_SEMANTIC,
) -> None:
    """Take mutants until the queue is empty. The pinned scope is checked at every mutant boundary: an edit of one
    of its files stops the campaign right away instead of after a check at the end."""
    lease = _Lease(
        daemon=daemon,
        results=results,
        progress=progress,
        timing=timing,
        costs=costs,
        store=store,
        cross_kill=cross_kill,
        mutant_timeout=mutant_timeout,
        finalize_timeout=finalize_timeout,
        detailed=detailed,
        diags=diags,
        killers_by_key=killers_by_key,
        defer_untouched=defer_untouched,
        reject_untouched=reject_untouched,
        reject_unstable_kill=reject_unstable_kill,
        fresh_workers=fresh_workers,
        kill_validity=kill_validity,
        semantic=semantic,
    )
    while True:
        if scope_watch is not None and (changed := scope_watch.changed()):
            campaign_scope_abort(fatal_errors, changed)
            return
        try:
            item = pending.get_nowait()
        except queue.Empty:
            return
        try:
            lease.run(item)
        except CampaignExactnessError as exc:
            if fatal_errors is None:
                raise
            fatal_errors.put(exc)
            return
        except DaemonStaleError as exc:
            # A stale answer is most often a source edit: the daemon reloads the new code and later mutants would mix
            # revisions. A changed scope aborts the campaign; otherwise it is environment drift and a requeue.
            changed = scope_watch.changed(force=True) if scope_watch is not None else ()
            if changed:
                campaign_scope_abort(fatal_errors, changed)
                return
            requeue_after_failure(daemon, item, exc, pending)
            return
        except (OSError, RuntimeError) as exc:
            requeue_after_failure(daemon, item, exc, pending)
            return


@dataclass(slots=True)
class _Lease:
    """The verdict of one mutant on one daemon."""

    daemon: Daemon
    results: ResultsByIndex
    progress: CampaignProgress
    timing: Timing
    costs: CostMap
    store: Store | None
    cross_kill: bool
    mutant_timeout: float
    finalize_timeout: bool
    detailed: bool
    diags: Diagnostics | None
    killers_by_key: KillersByKey | None
    defer_untouched: queue.Queue[Work] | None
    reject_untouched: bool
    reject_unstable_kill: bool
    fresh_workers: bool
    kill_validity: KillValidity | None
    semantic: SemanticPatterns

    def _persist(self, item: Work, result: MutantResult, killed_by: list[str], kill: KillRecord | None) -> None:
        if self.store is not None:
            persist_verdict(self.store, item, result, killed_by, kill=kill, semantic=self.semantic)

    def _terminal(self, item: Work, result: MutantResult, killed_by: list[str], kill: KillRecord | None = None) -> None:
        self._persist(item, result, killed_by, kill)
        self.progress.advance(_label(item), result.status, result.key)
        self.results[item.index] = result

    def _defer(self, item: Work, status: MutantStatus) -> None:
        """A provisional verdict, confirmed later in a fresh fork."""
        self.results[item.index] = MutantResult(item.mutant, status, len(item.covering), item.key, item.covering)
        if self.defer_untouched is not None:
            self.defer_untouched.put(item)

    def run(self, item: Work) -> None:
        daemon = self.daemon
        clock = time.monotonic
        t_write = clock()
        # The plugin marks a mutant it could not apply, an entry into the mutated code and the functions the lease
        # entered: cleared before the request (left by the previous mutant on this daemon), read after it.
        markers = _Markers.of(daemon)
        markers.clear()
        write_request_spec(daemon.mutant_file, item.request_spec())
        t_request = clock()
        predicted = self.killers_by_key.get(item.key, _EMPTY) if self.killers_by_key is not None else _EMPTY
        budget = mutant_timeout_budget(self.mutant_timeout, item.covering, self.costs)
        try:
            sub = run_subset_guarded(
                daemon,
                list(item.covering),
                self.costs,
                full=self.cross_kill,
                timeout=budget,
                label=_label(item),
                detailed=self.detailed,
                predicted_killers=predicted,
                fresh_workers=self.fresh_workers,
            )
        except CampaignExactnessError:
            # The mutant broke the worker protocol itself (no report, no active item): it cannot be judged, so it is
            # unsupported rather than a campaign-wide exactness abort. A paired original still aborts (the suite
            # itself is broken then).
            self._terminal(
                item,
                MutantResult(item.mutant, MutantStatus.UNSUPPORTED, len(item.covering), item.key, item.covering),
                [],
            )
            self.timing.add(t_request - t_write, clock() - t_request, {})
            return
        if isinstance(sub, Hang):
            self._hang(item, sub, markers, budget=budget, started=(t_write, t_request))
            return
        status = (
            MutantStatus.UNSUPPORTED
            if markers.unsupported.exists()
            else (MutantStatus.SURVIVED if sub.rc == 0 else MutantStatus.KILLED)
        )
        if sub.process_failures and (sub.rc == 0 or sub.killed_by):
            # A worker died mid-run while the reports stayed consistent: the verdict cannot be proven either way.
            # A mutant of the harness's own runtime (dogfooding mutates kill_deps and friends) crashes the worker
            # the way an application test never could, so the campaign treats this as an unsupported verdict
            # instead of aborting.
            self._terminal(
                item,
                MutantResult(item.mutant, MutantStatus.UNSUPPORTED, len(item.covering), item.key, item.covering),
                [],
            )
            self.timing.add(t_request - t_write, clock() - t_request, sub.durations)
            return
        rejected_kill = False
        reason = VerdictReason.NONE
        original_process_failure = False
        if status is MutantStatus.KILLED:
            verified = self._verify_kill(item, sub, markers, predicted=predicted, budget=budget)
            if verified is None:  # the paired original hung: handled as a timeout
                self.timing.add(t_request - t_write, clock() - t_request, sub.durations)
                return
            status, reason, rejected_kill, original_process_failure = verified
        t_end = clock()
        if rejected_kill and self.defer_untouched is not None:
            self._defer(item, status)
            self.timing.add(t_request - t_write, t_end - t_request, sub.durations)
            return
        touched = markers.touched.exists()
        if (
            status is MutantStatus.KILLED
            and self.defer_untouched is not None
            and not self.fresh_workers
            and not touched
        ):
            # A green paired original proves causality only when the mutated code object actually ran. A kill without
            # the touch marker is re-checked in a fresh process; touch plus a green original is an exact A/B verdict.
            self._defer(item, status)
            self.timing.add(t_request - t_write, t_end - t_request, sub.durations)
            return
        if status is MutantStatus.SURVIVED and markers.stale.exists():
            # A covering test ran the original code at the site: a closure or a copy of the function made before the
            # mutant (a decorator's wrapper built when a test module was imported) keeps the original body, so the
            # green run never tested the mutant there, touched or not. A fresh fork inherits the same objects.
            status = MutantStatus.UNSUPPORTED
        untouched_survivor = status is MutantStatus.SURVIVED and not touched and not rejected_kill
        if untouched_survivor and self.defer_untouched is not None:
            # A warm session fixture may have computed an object on the original in an earlier request: such a green
            # run does not prove survival. Exactly this mutant goes to the fork-per-request confirmation.
            self._defer(item, status)
            self.timing.add(t_request - t_write, t_end - t_request, sub.durations)
            return
        if untouched_survivor and self.reject_untouched:
            # Even a fresh process never entered the target code object: coverage does not prove the site is
            # reachable, so a green verdict is not a survivor.
            status = MutantStatus.UNSUPPORTED
        if status is MutantStatus.KILLED and not touched and self.reject_untouched:
            # A fresh fork did not enter the mutated code either: a process-level failure has no causal link to
            # the site.
            status = MutantStatus.UNSUPPORTED
            sub.killed_by.clear()
        # The kill dependencies are both leases: the mutant and the paired original append to one file. A
        # process-level failure of either cuts the record short; a multi-worker daemon splits the request.
        recorded = (
            kill_deps_read(markers.deps)
            if status is MutantStatus.KILLED
            and not sub.process_failures
            and not original_process_failure
            and daemon.workers == 1
            else None
        )
        # A compiled mutant carries the module name in co_filename, so the recorder never sees it: its own file (its
        # constants and imports live in the skeleton) joins the kill dependencies explicitly. Their order is free:
        # the store's encoding and the validity hash both sort them.
        deps = tuple({*recorded, (item.mutant.path, item.qualname)}) if recorded else None
        result = MutantResult(
            item.mutant,
            status,
            len(item.covering),
            item.key,
            item.covering,
            tuple(sub.killed_by),
            reason,
        )
        self._terminal(item, result, sub.killed_by, self._kill_record(deps))
        self.timing.add(t_request - t_write, t_end - t_request, sub.durations)
        if self.diags is not None:
            self.diags.add(_diag(item, result, sub, request_s=t_end - t_request, write_s=t_request - t_write))

    def _kill_record(self, deps: KillDeps | None) -> KillRecord | None:
        """What the cache keeps a kill by; None without recorded dependencies, without a validity hasher (a campaign
        without a store) or when a dependency changed during the campaign."""
        if deps is None or self.kill_validity is None:
            return None
        # Hashed right after the observation: an edit mid-campaign costs a re-run, never a false cache hit.
        validity = self.kill_validity.of(deps)
        return KillRecord(deps, validity) if validity else None

    def _verify_kill(
        self, item: Work, sub: RunResult, markers: _Markers, *, predicted: frozenset[str], budget: float
    ) -> tuple[MutantStatus, VerdictReason, bool, bool] | None:
        """Re-run the observed killers on the immediately restored original, in the same state.

        Accumulated session state can make a normally green test fail after many leases: the kill is proven only for
        killers that stay green on the original. A process-level kill is accepted only when pytest-fast proves the
        same active item that follows the complete compact-result prefix; rc 1 alone proves nothing.
        -> (status, reason, kill rejected, the original crashed), or None when the original hung."""
        daemon = self.daemon
        process_failure = bool(sub.process_failures)
        if process_failure:
            ordered = order_covering(list(item.covering), self.costs, predicted)
            killer = daemon_process_failure_nodeid(daemon, ordered, sub.durations, sub.process_failures)
            sub.killed_by.append(killer)
            if self.detailed:
                sub.phases.setdefault(killer, {})
        elif not sub.killed_by:
            raise CampaignExactnessError("pytest-fast rc=1 without a test report or active pytest item evidence")
        verification = list(sub.killed_by)
        write_original_spec(daemon.mutant_file, markers.deps)
        original = run_subset_guarded(
            daemon,
            verification,
            self.costs,
            full=True,
            timeout=budget,
            label=f"{_label(item)} [original]",
            # A process-level kill ends the persistent worker: the original needs a new process, or the request
            # would reach a dead pool without active-item evidence.
            fresh_workers=self.fresh_workers or process_failure,
        )
        if isinstance(original, Hang):
            self.progress.report_hang(item.key)
            respawn_daemon(daemon)
            result = MutantResult(
                item.mutant,
                MutantStatus.TIMEOUT,
                len(item.covering),
                item.key,
                item.covering,
                reason=VerdictReason.ORIGINAL_HANG,
            )
            if self.finalize_timeout:
                self._terminal(item, result, [])
            else:
                _note(f"timeout {_label(item)} [original]; sent to an isolated retry")
                self.results[item.index] = result
            return None
        sub.durations.update(original.durations)
        original_process_failure = bool(original.process_failures)
        if original.process_failures:
            original_order = order_covering(verification, self.costs, _EMPTY)
            daemon_process_failure_nodeid(daemon, original_order, original.durations, original.process_failures)
            # The active item and the whole unexecuted suffix are not proven green on the original: they cannot
            # confirm a kill, even if earlier verification items finished.
            original.killed_by.extend(
                nodeid
                for nodeid in process_failure_suffix(original_order, original.durations)
                if nodeid not in original.killed_by
            )
        elif original.rc != 0 and not original.killed_by:
            raise CampaignExactnessError("a paired original rc=1 without a test report or active pytest item evidence")
        unstable = set(original.killed_by)
        confirmed = [killer for killer in sub.killed_by if killer not in unstable]
        if unstable and not (self.fresh_workers or process_failure):
            # A killer failed on the original in this worker: the persistent session carries state an earlier
            # request left behind (a mutant of the harness's own fixture hook breaks pytest's fixture bookkeeping
            # for good). Every later verdict of this daemon would inherit it, so the next mutant gets a new worker.
            retire_persistent_pool(daemon)
        if process_failure and original.rc == 0:
            _note(f"process-kill {_label(item)}: {sub.killed_by[0]}, mutant rc=1 without a report, original green")
            return MutantStatus.KILLED, VerdictReason.NONE, False, original_process_failure
        if not confirmed:
            # An unstable killer (it fails on the original too) proves neither a kill nor survival. The report must
            # name the reason, or such a timeout is indistinguishable from a hanging mutant.
            sub.killed_by.clear()
            killers = ", ".join(sorted(unstable)) or "process-level rc=1"
            outcome = "verdict unresolved" if self.reject_unstable_kill else "fresh confirmation"
            _note(f"original-failure {_label(item)}: {killers}; the kill does not count, {outcome}")
            if self.reject_unstable_kill:
                return MutantStatus.TIMEOUT, VerdictReason.ORIGINAL_FAILED, True, original_process_failure
            return MutantStatus.SURVIVED, VerdictReason.NONE, True, original_process_failure
        sub.killed_by[:] = confirmed
        return MutantStatus.KILLED, VerdictReason.NONE, False, original_process_failure

    def _hang(self, item: Work, hang: Hang, markers: _Markers, *, budget: float, started: tuple[float, float]) -> None:
        """No observable rc: the request stalled."""
        t_write, t_request = started
        # The weight of a hanging mutant rises to the hang budget, so the ETA grows while it hangs.
        self.progress.report_hang(item.key)
        # The touch marker is written on the first entry, before the hang and the respawn (which keeps it).
        touched = markers.touched.exists()
        respawn_daemon(self.daemon)
        if not self.finalize_timeout:
            self.results[item.index] = MutantResult(
                item.mutant, MutantStatus.TIMEOUT, len(item.covering), item.key, item.covering
            )
            _note(f"timeout {_label(item)}; sent to an isolated retry")
            self.timing.add(t_request - t_write, time.monotonic() - t_request)
            return
        killer, reason = hang_verdict(
            self.daemon, hang, touched=touched, costs=self.costs, timeout=budget, label=_label(item)
        )
        if killer is not None:
            result = MutantResult(
                item.mutant, MutantStatus.KILLED, len(item.covering), item.key, item.covering, (killer,), reason
            )
        else:
            result = MutantResult(
                item.mutant, MutantStatus.TIMEOUT, len(item.covering), item.key, item.covering, reason=reason
            )
        self._terminal(item, result, list(result.killed_by))
        self.timing.add(t_request - t_write, time.monotonic() - t_request)


def _label(item: Work) -> str:
    return f"{item.mutant.path}:{item.mutant.lineno}"


def _diag(item: Work, result: MutantResult, sub: RunResult, *, request_s: float, write_s: float) -> MutantDiag:
    phases = sub.phases.values()
    return MutantDiag(
        key=item.key,
        path=item.mutant.path,
        lineno=item.mutant.lineno,
        operator=item.mutant.operator,
        status=result.status.value,
        covering=len(item.covering),
        tests_run=len(sub.phases),
        setup_s=sum(phase.get("setup", 0.0) for phase in phases),
        call_s=sum(phase.get("call", 0.0) for phase in phases),
        teardown_s=sum(phase.get("teardown", 0.0) for phase in phases),
        request_s=request_s,
        write_s=write_s,
        killer=sub.killed_by[0] if sub.killed_by else "",
    )
