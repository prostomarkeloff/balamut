"""The pytest-fast side of a campaign: resident daemons, requests and the no-progress watchdog.

Each daemon collects the suite once with the mutation plugin (``-p balamut.runtime.plugin``) and keeps a warm
worker pool across requests. Per mutant the driver writes a spec into the daemon's mutant file and requests only
the covering tests, killer first and stop-first: rc 1 is a candidate kill, rc 0 a candidate survivor. The verdict
logic itself (paired original, fresh confirmation, hang causality) lives in ``worker``.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol, cast

from pytest_fast import RunResult as CompactResult
from pytest_fast import request_run_results

from balamut.core.hashing import sha
from balamut.engine.results import CampaignExactnessError, CampaignSetupError, DaemonStaleError
from balamut.isolation import EnvMap, EnvOverlays
from balamut.runtime.kill_deps import KILL_DEPS_SUFFIX
from balamut.runtime.plugin import (
    KILL_DEPS_ENV,
    MUTANT_FILE_ENV,
    TOUCHED_MARKER_SUFFIX,
    UNSUPPORTED_MARKER_SUFFIX,
    MutantSpec,
)

__all__ = [
    "COLLECTION_FILE_LIMIT",
    "CompactResult",
    "CostMap",
    "Daemon",
    "DaemonProcess",
    "Hang",
    "PhaseMap",
    "RunResult",
    "adopt_pool",
    "await_pool",
    "campaign_addopts",
    "daemon_process_failure_nodeid",
    "kill_stale_daemon",
    "killpg",
    "order_covering",
    "process_failure_suffix",
    "request",
    "respawn_daemon",
    "run_subset",
    "run_subset_guarded",
    "shutdown_pool",
    "spawn_daemon",
    "spawn_pool",
    "switch_pool_to_fresh_workers",
    "wait_ready",
    "write_original_spec",
    "write_request_spec",
]

type CostMap = dict[str, float]
type PhaseMap = dict[str, dict[str, float]]  # node id -> {setup|call|teardown: seconds}, only with --detailed
type Frame = dict[str, object]  # the final transport frame of pytest-fast
type _RunMeta = dict[str, object]

_EMPTY: frozenset[str] = frozenset()
_OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "xfailed", "xpassed"})
_FAILED = frozenset({"failed", "error"})
_PHASES = frozenset({"setup", "call", "teardown"})
_READY_TIMEOUT = 180.0
# The idle TTL of a daemon bounds the life of an orphan (the driver killed past its teardown). A lane can outlast
# it for pool members that drained their share early: a clean TTL exit is stale, not fatal — await_pool respawns
# the daemon. The profile overrides the TTL through `daemon_ttl` (None keeps this built-in default).
_IDLE_TTL_S = 600
_CONNECT_ATTEMPTS = 50
_CONNECT_RETRY_DELAY = 0.1
_HANG_WARN_FRACTION = 0.75
# Daemons collect only the files of the campaign's covering tests. Above the limit the whole suite is collected:
# the paths travel in PYTEST_ADDOPTS, and an environment variable has a size ceiling.
COLLECTION_FILE_LIMIT = 400


class DaemonProcess(Protocol):
    """What the driver needs of a daemon process (a ``subprocess.Popen`` in production)."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(slots=True)
class Daemon:
    address: str
    mutant_file: Path
    proc: DaemonProcess
    log: IO[bytes]
    workers: int
    env: EnvMap
    persist_workers: bool = True
    idle_ttl: int = _IDLE_TTL_S  # a respawn keeps the configured TTL
    respawns: int = 0  # every hard reset gets a new address, so an orphaned thread cannot reconnect


class _AdoptedProcess:
    """The process surface of a daemon adopted from an earlier run: pid-directed, never a Popen handle."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return 1
        return None

    def terminate(self) -> None:
        killpg(self)

    def kill(self) -> None:
        killpg(self)

    def wait(self, timeout: float | None = None) -> int:
        deadline = math.inf if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("adopted daemon", 0.0 if timeout is None else timeout)
            time.sleep(0.05)
        return 0


@dataclass(slots=True)
class RunResult:
    rc: int
    durations: CostMap  # node id -> setup + call + teardown seconds, freshly measured (for the cost store)
    killed_by: list[str]  # node ids of the tests that failed on this mutant
    phases: PhaseMap = field(default_factory=dict[str, dict[str, float]])
    process_failures: list[str] = field(default_factory=list[str])  # the active item from pytest-fast crash evidence


@dataclass(frozen=True, slots=True)
class Hang:
    """A no-progress timeout: ``ordered`` is the execution order of the covering tests, ``completed`` the ones that
    reported before the request stalled."""

    ordered: tuple[str, ...]
    completed: frozenset[str]

    def hung_nodeid(self) -> str | None:
        """The test a sequential request stalled on: the first in execution order without a report.

        None when the reported tests are not a prefix of the order (the response was not sequential, or the
        evidence is incomplete) or every test reported: the hung test is then unknown and the hang proves nothing."""
        done = len(self.completed)
        if done >= len(self.ordered) or frozenset(self.ordered[:done]) != self.completed:
            return None
        return self.ordered[done]


def write_original_spec(path: Path, deps_path: Path) -> None:
    """The spec of a paired original run: no mutation, but its lease adds its functions to the kill dependencies."""
    path.write_text(json.dumps({"request_token": uuid.uuid4().hex, "original_deps_path": str(deps_path)}), "utf-8")


def write_request_spec(path: Path, spec: MutantSpec) -> None:
    """Write ``spec`` with a unique token for the current mutant lease, leaving the work item untouched."""
    request_spec: MutantSpec = {
        **spec,
        "request_token": uuid.uuid4().hex,
        "touch_path": f"{path}{TOUCHED_MARKER_SUFFIX}",
        "deps_path": f"{path}{KILL_DEPS_SUFFIX}",
    }
    path.write_text(json.dumps(request_spec), encoding="utf-8")


def _record_result(result: CompactResult, durations: CostMap, failures: list[str], phases: PhaseMap) -> None:
    nodeid = result["nodeid"]
    duration = result["duration"]
    if result["outcome"] not in _OUTCOMES or not math.isfinite(duration) or duration < 0:
        raise RuntimeError("pytest-fast returned a malformed compact result")
    if result["outcome"] in _FAILED:
        failures.append(nodeid)
    result_phases = result.get("phases")
    if result_phases is None:
        durations[nodeid] = max(durations.get(nodeid, 0.0), float(duration))
        return
    node_phases = phases.setdefault(nodeid, {})
    for when, seconds in result_phases.items():
        if when not in _PHASES or not math.isfinite(seconds) or seconds < 0:
            raise RuntimeError("pytest-fast returned malformed phases in a compact result")
        node_phases[when] = max(node_phases.get(when, 0.0), float(seconds))
    durations[nodeid] = max(durations.get(nodeid, 0.0), sum(node_phases.values()))


def _process_failure_evidence(frame: Frame, nodeids: list[str] | None) -> list[str]:
    """The active pytest items of crashed workers, validated against the requested subset."""
    raw_meta = frame.get("run_meta")
    # The frame is plain builtins decoded by pytest-fast; run_meta is a str-keyed dict when present.
    meta: _RunMeta = cast("_RunMeta", raw_meta) if isinstance(raw_meta, dict) else {}
    raw = meta.get("process_failures")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeError("pytest-fast returned malformed process-failure evidence")
    evidence = [nodeid for nodeid in cast("list[str | int]", raw) if isinstance(nodeid, str)]
    if len(evidence) != len(cast("list[str | int]", raw)):
        raise RuntimeError("pytest-fast returned malformed process-failure evidence")
    if len(evidence) != len(set(evidence)):
        raise RuntimeError("pytest-fast returned duplicate process-failure evidence")
    if nodeids is not None and not set(evidence) <= set(nodeids):
        raise RuntimeError("pytest-fast returned a process-failure node id outside the requested subset")
    return evidence


def request(
    address: str,
    nodeids: list[str] | None,
    durations: CostMap,
    failures: list[str],
    progress_clock: list[float] | None = None,
    phases: PhaseMap | None = None,
    *,
    process_failures: list[str] | None = None,
    stop_on_failure: bool,
    fresh_workers: bool = False,
) -> int:
    """One request through the compact protocol -> rc 0 (passed) or 1 (a test failed or a worker died).

    Durations of every reported test and the node ids of the failed ones are captured; ``progress_clock[0]`` is
    bumped on every progress frame (the no-progress watchdog reads it). rc 1 with neither a failing report nor
    active-item evidence proves nothing and fails closed. Other return codes (interrupt, internal, usage, no tests)
    are not kills: the caller requeues the mutant."""
    if stop_on_failure and fresh_workers:
        raise RuntimeError("stop_on_failure and fresh_workers cannot be combined")
    observed_phases: PhaseMap = phases if phases is not None else {}
    observed_process_failures = process_failures if process_failures is not None else []

    def on_progress(_done: int, _total: int) -> None:
        if progress_clock is not None:
            progress_clock[0] = time.monotonic()

    def on_result(result: CompactResult) -> None:
        _record_result(result, durations, failures, observed_phases)

    for _ in range(_CONNECT_ATTEMPTS):
        try:
            frame: Frame = request_run_results(
                address,
                on_result,
                on_progress=on_progress,
                nodeids=nodeids,
                stop_on_failure=stop_on_failure,
                fresh_workers=fresh_workers,
            )
        except OSError:
            # A short race: the socket exists before the accept loop is ready.
            time.sleep(_CONNECT_RETRY_DELAY)
            continue
        if frame.get("stale"):
            # The daemon drifted from the client (environment or sources): no test ran. Counting rc 1 as a kill
            # would inflate the score; the worker requeues and the daemon reloads on its next request.
            raise DaemonStaleError(f"pytest-fast answered stale on {address}: the mutant did not run")
        rc = frame.get("rc")
        if rc not in {0, 1}:
            raise RuntimeError(
                f"pytest-fast rc={rc} on {address} (collection, internal, usage or no tests): not a kill"
            )
        observed_process_failures.extend(_process_failure_evidence(frame, nodeids))
        if rc == 1 and not failures and not observed_process_failures:
            raise CampaignExactnessError(
                "pytest-fast rc=1 without a test report and without a proven active pytest item"
            )
        return 1 if rc == 1 else 0
    raise RuntimeError(f"could not run the subset on {address}: the daemon is unreachable")


def order_covering(nodeids: list[str], costs: CostMap, predicted_killers: AbstractSet[str]) -> list[str]:
    """One killer-first order for a persistent stop-first request.

    Known killers go before the rest; within both groups the order is by cost, then node id. Duplicates are
    dropped: repeating a test adds no information but pays its fixtures again. The prediction never affects the
    verdict: on a miss the request still reaches every covering test."""
    unique = set(nodeids)

    def by_cost(nodeid: str) -> tuple[float, str]:
        return costs.get(nodeid, 0.0), nodeid

    predicted = sorted(unique & predicted_killers, key=by_cost)
    rest = sorted(unique - predicted_killers, key=by_cost)
    return [*predicted, *rest]


def process_failure_suffix(ordered: list[str], durations: CostMap) -> list[str]:
    """The suffix of a sequential request that its compact results did not confirm."""
    completed = set(durations)
    if completed != set(ordered[: len(completed)]) or len(completed) >= len(ordered):
        raise RuntimeError("pytest-fast did not prove the exact node id of a process-level failure")
    return ordered[len(completed) :]


def daemon_process_failure_nodeid(daemon: Daemon, ordered: list[str], durations: CostMap, evidence: list[str]) -> str:
    """Check the active-item evidence and name the canonical process-level killer."""
    if daemon.workers != 1:
        raise CampaignExactnessError(
            f"a pytest-fast process-level failure with workers={daemon.workers} does not prove a canonical killer"
        )
    nodeid = process_failure_suffix(ordered, durations)[0]
    if evidence != [nodeid]:
        observed = ", ".join(evidence) or "no evidence"
        raise CampaignExactnessError(
            f"the pytest-fast active item did not match the sequential prefix: expected {nodeid}, got {observed}"
        )
    return nodeid


def run_subset(
    address: str,
    nodeids: list[str],
    costs: CostMap,
    *,
    full: bool = False,
    progress_clock: list[float] | None = None,
    detailed: bool = False,
    predicted_killers: AbstractSet[str] = _EMPTY,
    fresh_workers: bool = False,
    durations: CostMap | None = None,
) -> RunResult:
    """Run the covering tests in one ordered persistent request.

    The default is exact stop-first: pytest-fast stops handing out items after the first real failure. ``full``
    keeps an exhaustive cross-kill. ``fresh_workers`` runs exhaustively in a new fork while keeping the daemon's
    collection (incompatible with stop-first). ``durations`` is an external sink: the watchdog reads it to see which
    tests finished before a stall."""
    ordered = order_covering(nodeids, costs, predicted_killers)
    durations = {} if durations is None else durations
    killed_by: list[str] = []
    process_failures: list[str] = []
    phases: PhaseMap | None = {} if detailed else None
    if not ordered:
        return RunResult(0, durations, killed_by, phases or {}, process_failures)
    rc = request(
        address,
        ordered,
        durations,
        killed_by,
        progress_clock,
        phases,
        process_failures=process_failures,
        stop_on_failure=not full and not fresh_workers,
        fresh_workers=fresh_workers,
    )
    return RunResult(rc, durations, killed_by, phases or {}, process_failures)


def run_subset_guarded(
    daemon: Daemon,
    nodeids: list[str],
    costs: CostMap,
    *,
    full: bool,
    timeout: float,
    label: str,
    detailed: bool = False,
    predicted_killers: AbstractSet[str] = _EMPTY,
    fresh_workers: bool = False,
) -> RunResult | Hang:
    """``run_subset`` under a no-progress timeout: a hung mutant (an endless loop or await in a forked worker) does
    not stop the campaign.

    The request runs in a thread that bumps a clock on every report; no new report for ``timeout`` seconds returns
    a ``Hang`` with the tests that did report (the caller kills and respawns the daemon). It is no-progress, not
    total time: an exhaustive cross-kill over many slow tests is legitimately long but keeps reporting. A real
    daemon failure (OSError, RuntimeError) is re-raised."""
    box: list[RunResult] = []
    errors: list[OSError | RuntimeError] = []
    clock = [time.monotonic()]
    reported: CostMap = {}

    def target() -> None:
        try:
            box.append(
                run_subset(
                    daemon.address,
                    nodeids,
                    costs,
                    full=full,
                    progress_clock=clock,
                    detailed=detailed,
                    predicted_killers=predicted_killers,
                    fresh_workers=fresh_workers,
                    durations=reported,
                )
            )
        except (OSError, RuntimeError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    warned = False
    while thread.is_alive():
        thread.join(1.0)
        stalled = time.monotonic() - clock[0]
        if thread.is_alive() and not warned and stalled >= timeout * _HANG_WARN_FRACTION:
            sys.stderr.write(f"\n  {label} stalled for {stalled:.0f}s; killed at {timeout:.0f}s\n")
            sys.stderr.flush()
            warned = True
        if thread.is_alive() and stalled > timeout:
            # dict.copy() is atomic with respect to the writer thread: a report arriving right now is harmless.
            return Hang(
                ordered=tuple(order_covering(nodeids, costs, predicted_killers)), completed=frozenset(reported.copy())
            )
    if errors:
        raise errors[0]
    if not box:
        # The thread ended with neither a result nor a recorded failure: run_subset raised another exception type
        # (a protocol or decoding failure, an error in a callback). That is not a hang; the worker requeues.
        raise RuntimeError(f"run_subset on {daemon.address} ended without a result: an uncaught harness exception")
    return box[0]


def campaign_addopts(base: str, covering: Iterable[str], *, hang_dump_after: float) -> str:
    """PYTEST_ADDOPTS of the campaign daemons: collect only the covering test files, dump the stacks of a hung test.

    The paths travel in the environment, which pytest-fast fingerprints: the client must hold the same addopts as
    the daemons, or every request is answered stale. ``faulthandler_timeout`` makes pytest dump the stacks of all
    threads into the daemon log when a test runs past the threshold, so a hang leaves a trace."""
    files = sorted({nodeid.partition("::")[0] for nodeid in covering})
    parts = [base.strip(), f"-o faulthandler_timeout={math.ceil(hang_dump_after)}"]
    if files and len(files) <= COLLECTION_FILE_LIMIT:
        parts.append(shlex.join(files))
    return " ".join(part for part in parts if part)


def spawn_daemon(
    address: str,
    mutant_file: Path,
    workers: int,
    env: EnvMap,
    log: IO[bytes],
    *,
    persist_workers: bool = True,
    idle_ttl: int = _IDLE_TTL_S,
) -> subprocess.Popen[bytes]:
    """Start a resident daemon; the mutant file and the kill-dependency switch are not fingerprinted.

    ``idle_ttl`` is the seconds of idleness pytest-fast tolerates before a clean exit: an orphan never outlives
    it, and the campaign treats the exit as stale rather than fatal."""
    spawn_env = {
        **env,
        "PYTHONWARNINGS": "ignore::DeprecationWarning",
        MUTANT_FILE_ENV: str(mutant_file),
        KILL_DEPS_ENV: "1",
    }
    argv = [
        sys.executable,
        "-m",
        "pytest_fast",
        "--serve",
        "--address",
        address,
        "--ttl",
        str(idle_ttl),
        "--workers",
        str(workers),
    ]
    # Persistent workers keep session fixtures warm across requests. The plugin keeps the verdict identical to a
    # non-persistent run: the edit is reversible and a new mutant file reverts the previous mutant.
    if persist_workers:
        argv.append("--persist-workers")
    # A session of its own: killpg then hits exactly the daemon and its forkserver workers, never the driver.
    return subprocess.Popen(argv, env=spawn_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def killpg(proc: DaemonProcess) -> None:
    """SIGKILL the daemon's process group (the daemon and its forkserver workers), falling back to the daemon."""
    if proc.pid > 0:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.kill()


def kill_stale_daemon(address: str) -> None:
    """Remove the daemon a previous, killed run left on this socket.

    pytest-fast writes its pid to ``<address>.pid``. The command line is checked for pytest_fast and this exact
    address before the kill, so a reused pid of another process is never touched."""
    pid_file = Path(f"{address}.pid")
    raw = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
    pid_file.unlink(missing_ok=True)
    ps = shutil.which("ps")
    if not raw.isdigit() or ps is None:
        return
    probe = subprocess.run([ps, "-p", raw, "-o", "command="], capture_output=True, text=True, check=False)
    if "pytest_fast" not in probe.stdout or address not in probe.stdout:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(int(raw)), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(int(raw), signal.SIGKILL)
    sys.stderr.write(f"removed an orphaned daemon pid {raw} on {address} (an interrupted earlier run)\n")


def wait_ready(address: str, proc: DaemonProcess, deadline: float) -> None:
    """Wait until the daemon binds its socket, or fail clearly when it dies first."""
    socket_path = Path(address)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the pytest-fast daemon died (exit {proc.returncode}); see its log")
        if socket_path.exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"the daemon did not bind {address} in time")


def spawn_pool(
    address_base: str,
    daemons: int,
    workers: int,
    env: EnvMap,
    overlays: EnvOverlays,
    *,
    scratch: Path,
    persist_workers: bool = True,
    idle_ttl: int | None = None,
) -> list[Daemon]:
    """Spawn ``daemons`` daemons; daemon ``k`` runs with ``overlays[k]`` (its isolation slot) when there are any.

    Spec and log files are keyed by the address base, so two campaigns on one host never share a mutant file.
    ``idle_ttl`` is the configured daemon TTL; None keeps the built-in default."""
    if overlays and len(overlays) < daemons:
        raise CampaignSetupError(f"{daemons} daemons need {daemons} isolation slots, got {len(overlays)}")
    ttl = idle_ttl if idle_ttl is not None else _IDLE_TTL_S
    scratch.mkdir(parents=True, exist_ok=True)
    run_tag = sha(address_base)[:8]
    pool: list[Daemon] = []
    for index in range(daemons):
        mutant_file = scratch / f"mutant-{run_tag}-{index}.json"
        mutant_file.write_text("", encoding="utf-8")  # empty: the daemon collects the original
        log = (scratch / f"daemon-{run_tag}-{index}.log").open("wb")
        address = f"{address_base}.{index}"
        kill_stale_daemon(address)
        Path(address).unlink(missing_ok=True)  # a stale socket file would look ready before the new daemon binds
        daemon_env = {**env, **overlays[index]} if overlays else env
        proc = spawn_daemon(
            address, mutant_file, workers, daemon_env, log, persist_workers=persist_workers, idle_ttl=ttl
        )
        pool.append(Daemon(address, mutant_file, proc, log, workers, daemon_env, persist_workers, idle_ttl=ttl))
    note = " with isolated slots" if overlays else " sharing the environment"
    sys.stderr.write(f"daemons: {daemons} x {workers} workers{note} (logs {scratch}/daemon-{run_tag}-*.log)\n")
    return pool


def await_pool(pool: list[Daemon]) -> None:
    """Wait for every pool member to bind its socket; respawn a member that exited cleanly (its idle TTL).

    A lane reuses the pool, so a member may have idled out since the previous lane: a clean exit 0 is stale,
    not fatal. An unclean exit or a missed deadline raises."""
    deadline = time.monotonic() + _READY_TIMEOUT
    for daemon in pool:
        if daemon.proc.poll() == 0:
            sys.stderr.write(f"daemon {daemon.address} idled out (exit 0); respawning it\n")
            respawn_daemon(daemon)
            continue
        if daemon.proc.poll() is not None:
            raise RuntimeError(f"the pytest-fast daemon died (exit {daemon.proc.returncode}); see its log")
        try:
            wait_ready(daemon.address, daemon.proc, deadline)
        except RuntimeError:
            if daemon.proc.poll() == 0:  # the TTL fired while the daemon was still alive at the first poll
                sys.stderr.write(f"daemon {daemon.address} idled out (exit 0); respawning it\n")
                respawn_daemon(daemon)
            else:
                raise


def adopt_pool(
    address_base: str, daemons: int, workers: int, env: EnvMap, scratch: Path, *, persist_workers: bool = True
) -> list[Daemon] | None:
    """Adopt the daemons of a persisted earlier run at the fixed addresses, or None when any is gone.

    Each adopted daemon must show its pid file, a live pid and its bound socket; the first gap or dead daemon
    invalidates the whole pool (the caller then spawns fresh). The workers and the environment are the caller's
    current plan: the daemon answers stale and reloads on its first request when the environment drifted."""
    run_tag = sha(address_base)[:8]
    pool: list[Daemon] = []
    for index in range(daemons):
        address = f"{address_base}.{index}"
        pid_file = Path(f"{address}.pid")
        if not pid_file.exists() or not Path(address).exists():
            return None
        raw = pid_file.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return None
        pid = int(raw)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OverflowError:
            return None  # a pid beyond the range is a corrupt file, not a live daemon
        except PermissionError:
            pass  # another user owns the pid: still alive, still adoptable
        mutant_file = scratch / f"mutant-{run_tag}-{index}.json"
        log = (scratch / f"daemon-{run_tag}-{index}.log").open("ab")
        pool.append(Daemon(address, mutant_file, _AdoptedProcess(pid), log, workers, env, persist_workers))
    return pool


def shutdown_pool(pool: list[Daemon]) -> None:
    for daemon in pool:
        daemon.proc.terminate()
    for daemon in pool:
        try:
            daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            killpg(daemon.proc)
        daemon.log.close()
        daemon.mutant_file.write_text("", encoding="utf-8")
        for suffix in (UNSUPPORTED_MARKER_SUFFIX, TOUCHED_MARKER_SUFFIX, KILL_DEPS_SUFFIX):
            Path(f"{daemon.mutant_file}{suffix}").unlink(missing_ok=True)
        Path(daemon.address).unlink(missing_ok=True)
        Path(f"{daemon.address}.pid").unlink(missing_ok=True)


def respawn_daemon(daemon: Daemon) -> None:
    """Hard-reset a daemon after a hung mutant: kill it with the hung worker and start it on a NEW address.

    The new address matters: the orphaned timeout thread still retries the old one, and on the same socket it
    would reach the fresh daemon and run a stale subset next to the next mutant against a rewritten mutant file."""
    old_address = daemon.address
    killpg(daemon.proc)
    with contextlib.suppress(subprocess.TimeoutExpired):
        daemon.proc.wait(timeout=5)
    Path(old_address).unlink(missing_ok=True)
    Path(f"{old_address}.pid").unlink(missing_ok=True)
    daemon.respawns += 1
    head, _, tail = old_address.rpartition(".r")  # drop the previous .rN so the address does not grow
    base = head if head and tail.isdigit() else old_address
    daemon.address = f"{base}.r{daemon.respawns}"
    Path(daemon.address).unlink(missing_ok=True)
    daemon.mutant_file.write_text("", encoding="utf-8")
    daemon.proc = spawn_daemon(
        daemon.address,
        daemon.mutant_file,
        daemon.workers,
        daemon.env,
        daemon.log,
        persist_workers=daemon.persist_workers,
        idle_ttl=daemon.idle_ttl,
    )
    wait_ready(daemon.address, daemon.proc, time.monotonic() + _READY_TIMEOUT)


def switch_pool_to_fresh_workers(pool: list[Daemon]) -> None:
    """End the persistent pytest sessions while keeping every daemon's collection.

    An empty fresh-worker request is an acknowledged protocol barrier: pytest-fast destroys the warm pool, runs an
    empty fork-per-request run and acks. Every later ``fresh_workers`` request gets a new pytest session without
    another collection."""
    if not pool:
        return

    def switch(daemon: Daemon) -> None:
        daemon.mutant_file.write_text("", encoding="utf-8")
        for attempt in (0, 1):
            try:
                rc = request(daemon.address, [], {}, [], stop_on_failure=False, fresh_workers=True)
            except RuntimeError:
                # The daemon died on a mutant that crashed the process (a code-object corruption segfaults): the
                # switch is a barrier, not a verdict, so a fresh daemon on a new address satisfies it just as well.
                rc = None
            if rc == 0:
                return
            if attempt == 0:
                respawn_daemon(daemon)
        raise RuntimeError(f"pytest-fast did not switch {daemon.address} to fresh-worker mode (rc={rc})")

    with ThreadPoolExecutor(max_workers=len(pool), thread_name_prefix="balamut-fresh") as executor:
        list(executor.map(switch, pool))
