"""pytest plugin: applies an in-memory mutant inside a pytest-fast worker before the tests run.

Loaded into daemons with ``-p balamut.runtime.plugin``. The daemon collects the original once (an empty mutant
file means no mutation). For every request the driver writes a spec carrying a unique ``request_token`` before
sending it. A persistent worker reads it at the first ``pytest_runtest_setup`` of the request: that is the lease
boundary, where the worker undoes the reloads of the previous lease, clears the safe Python caches and prepares the
new mutant. ``sys.modules`` is never changed: re-importing a C extension is unsafe for process-global state, and
partially unloading a Python graph breaks the identity of singletons and registries.

A mutant is live only while a test item runs: ``pytest_runtest_setup`` applies it and ``pytest_runtest_protocol``
reverts it once the item's teardown is over. The plugin's own code therefore never runs a mutant's code, which
matters when balamut tests itself: a mutant of this module that stayed live across a boundary would decide how the
next mutant is applied and observed.

State lives in the worker session's ``stash``. The driver keeps the worker pool across requests, so
session-scoped fixtures and the imported graph are paid for once per campaign.
"""

from __future__ import annotations

import ast
import importlib
import json
import multiprocessing
import os
import sys
from collections.abc import Callable, Generator
from contextlib import suppress
from enum import Enum
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import NamedTuple, NotRequired, TypedDict, cast

import pytest

from balamut.concurrency import fuzz_globals
from balamut.core.mutants import ApplyMechanism, BcEdit, BcEditKind
from balamut.operators.oracle import oracle_globals
from balamut.runtime import kill_deps
from balamut.runtime.bytecode import mutate_code
from balamut.runtime.fault import fault_globals
from balamut.runtime.inmemory import compile_function_code, patch_code, resolve_with_cache_clear
from balamut.runtime.reloads import Follower, ReloadLog, reload_log
from balamut.runtime.worker_reset import WorkerBaseline, capture_baseline, reset_to_baseline

# None of these is fingerprinted by pytest-fast (only PYTEST_* keys and the project's own prefixes are): daemons
# with different mutant files must not restart each other as stale.
MUTANT_FILE_ENV = "BALAMUT_MUTANT_FILE"
# Set only for campaign daemons: the kill-dependency recorder is armed in their workers, not in any process that
# merely imports this module (tests, tools).
KILL_DEPS_ENV = "BALAMUT_KILL_DEPS"
TEST_ROOTS_ENV = "BALAMUT_TEST_ROOTS"
# Kill switch for the bytecode fast path (a safety fallback and an A/B lever).
FORCE_COMPILE_ENV = "BALAMUT_FORCE_COMPILE"

UNSUPPORTED_MARKER_SUFFIX = ".unsupported"
TOUCHED_MARKER_SUFFIX = ".touched"
STALE_MARKER_SUFFIX = ".stale"
_MONITORING_TOOL_IDS = (4, 3)  # 0/1/2/5 are reserved by CPython for debugger/coverage/profiler/optimizer
_ANNOTATE = "__annotate__"  # the deferred annotations of a definition (PEP 649)

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


type Revert = Callable[[], None]
type Apply = Callable[[], Revert]  # puts a prepared mutant into the process for one test item


class _Lease(NamedTuple):
    """The mutant lease of this (possibly persistent) worker: the key is the raw spec text (unique per request),
    ``apply`` puts the mutant into the process for one item of the lease and returns what reverts it."""

    key: str
    apply: Apply


_STATE = pytest.StashKey["_Lease | None"]()
# The revert of the mutant the current test item runs against, taken by ``pytest_runtest_protocol``.
_ITEM = pytest.StashKey["Revert | None"]()
# Baseline of the persistent worker: the module set gates lazily imported targets; the baseline `@lru_cache`
# wrappers are cleared at every mutant boundary. The import graph stays whole.
_BASELINE = pytest.StashKey[WorkerBaseline]()
# The recorder of functions a mutant lease entered (see kill_deps): one per worker, armed before collection.
_DEPS = pytest.StashKey[kill_deps.DepsRecorder]()
# The reloads the tests of a lease ran (see reloads): one log per worker, installed before collection.
_RELOADS = pytest.StashKey[ReloadLog]()


class MutantSite(TypedDict):
    """A mutant spec without the mutated module text. A campaign keeps this for every mutant and adds the text when
    it writes the request: a text kept per mutant would hold each file once for every mutant in it."""

    module: str
    qualname: str
    kind: str
    self_covered: NotRequired[bool]  # the mutant mutates its own covering test: patch the collected module
    mechanism: NotRequired[str]  # ApplyMechanism value; absent means CODE
    default_index: NotRequired[int]  # DEFAULTS: the slot in __defaults__
    default_name: NotRequired[str]  # DEFAULTS: the key in __kwdefaults__ (a keyword-only default)
    default_literal: NotRequired[str]  # DEFAULTS: text of the new value (ast.literal_eval)
    # Bytecode edit (const/binop/compare): the site position and the structured edit. Absent: compile right away.
    bc_kind: NotRequired[str]
    bc_source: NotRequired[str]
    bc_target: NotRequired[str]
    bc_lineno: NotRequired[int]
    bc_col: NotRequired[int]
    bc_end_col: NotRequired[int]
    bc_mutated: NotRequired[str]
    request_token: NotRequired[str]  # the unique boundary of a mutant lease
    target_lineno: NotRequired[int]  # site line: selects the exact nested code object for the touch marker
    target_col: NotRequired[int]  # UTF-8 byte column of the site start
    touch_path: NotRequired[str]  # marker of an actual entry into the mutated code object
    # Marker of an entry into the original code object at the site: a closure or a copy of the function made before
    # the mutant keeps the original body, so the test that called it never ran the mutant.
    stale_path: NotRequired[str]
    deps_path: NotRequired[str]  # where the lease publishes the repository functions it entered


class MutantSpec(MutantSite):
    mutated_source: str


_REQUIRED_SPEC_KEYS = ("module", "qualname", "mutated_source", "kind")
_OPTIONAL_STR_KEYS = (
    "mechanism",
    "default_name",
    "default_literal",
    "request_token",
    "touch_path",
    "stale_path",
    "deps_path",
    "bc_kind",
    "bc_source",
    "bc_target",
    "bc_mutated",
)
_OPTIONAL_INT_KEYS = (
    "default_index",
    "target_lineno",
    "target_col",
    "bc_lineno",
    "bc_col",
    "bc_end_col",
)
_OPTIONAL_BOOL_KEYS = ("self_covered",)


def parse_mutant_spec(raw: str) -> MutantSpec | None:
    """Parse and validate the untrusted spec text; None for non-JSON, a non-object, a missing required key or any
    value of the wrong type. Wrong types would otherwise crash the worker deep inside apply (an unhashable module
    name, a non-string qualname, a bool used as an index), so the driver gets UNSUPPORTED instead."""
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if any(not isinstance(parsed.get(key), str) for key in _REQUIRED_SPEC_KEYS):
        return None
    for key in _OPTIONAL_STR_KEYS:
        value = parsed.get(key)
        if value is not None and not isinstance(value, str):
            return None
    for key in _OPTIONAL_INT_KEYS:
        value = parsed.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return None
    for key in _OPTIONAL_BOOL_KEYS:
        value = parsed.get(key)
        if value is not None and not isinstance(value, bool):
            return None
    # The shape was validated key by key above; a TypedDict cannot be narrowed from a plain dict by isinstance.
    return cast("MutantSpec", parsed)


class ApplyOutcome(Enum):
    APPLIED = "applied"
    UNSUPPORTED = "unsupported"  # not applied (resolve / compile / freevars): never reported as survived


def _codes_at_site(root: CodeType, lineno: int | None, col: int | None) -> tuple[CodeType, ...]:
    """The innermost code objects whose own source extent holds the start of the edit; the root when none does.

    Only the start is compared: it is the same point in the original and in the mutated code, while the edit may
    have moved everything after it. A nested code object that the edit merely contains (a lambda argument of an edited
    call) does not hold the start, so the marker stays on the scope that runs the edit: armed on the lambda, it
    would stay silent for a mutant whose call fails before the lambda runs. The ``__annotate__`` function of an
    annotated nested definition (PEP 649) spans the whole definition but runs only when the annotations are read:
    it is never a candidate."""
    if lineno is None:
        return (root,)
    matches: list[tuple[int, CodeType]] = []

    def visit(code: CodeType, depth: int) -> None:
        if _extent_holds(code, lineno, col):
            matches.append((depth, code))
        for value in code.co_consts:
            if isinstance(value, CodeType) and value.co_name != _ANNOTATE:
                visit(value, depth + 1)

    visit(root, 0)
    if not matches:
        return (root,)
    deepest = max(depth for depth, _code in matches)
    return tuple(code for depth, code in matches if depth == deepest)


def _extent_holds(code: CodeType, lineno: int, col: int | None) -> bool:
    """Does the source extent of the code object's own instructions hold the point ``(lineno, col)``?

    Zero-width positions are skipped: they mark artificial instructions (``RESUME`` sits at column 0 of the
    definition line) and cover no source text. Without a column (an older spec, or code compiled without column
    positions) the lines decide."""
    lines: list[tuple[int, int]] = []
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for start, end, start_col, end_col in code.co_positions():
        if start is None or end is None:
            continue
        if start_col is None or end_col is None:
            lines.append((start, end))
        elif (start, start_col) != (end, end_col):
            lines.append((start, end))
            spans.append(((start, start_col), (end, end_col)))
    if col is not None and spans:
        return min(first for first, _last in spans) <= (lineno, col) < max(last for _first, last in spans)
    return bool(lines) and min(first for first, _last in lines) <= lineno <= max(last for _first, last in lines)


def _arm_touch_marker(
    codes: tuple[CodeType, ...],
    path: str | None,
    *,
    stale: tuple[CodeType, ...] = (),
    stale_path: str | None = None,
) -> Callable[[], None]:
    """Mark the first actual entry into the target code objects through a local ``sys.monitoring`` event.

    ``stale`` are the original code objects at the site: an entry into one of them marks ``stale_path``. Patching
    reaches the function the mutant names and every closure its new body creates, but not a closure (or a copy) made
    earlier, such as the wrapper a decorator built when a test module was imported: that one keeps the original
    body, and the test calling it never runs the mutant."""
    if path is None:
        return _noop_restore
    monitoring = sys.monitoring
    tool_id: int | None = None
    for candidate in _MONITORING_TOOL_IDS:
        try:
            monitoring.use_tool_id(candidate, "balamut-touch")
        except ValueError:  # the id is taken by another monitoring tool of this process
            continue
        tool_id = candidate
        break
    if tool_id is None:
        return _noop_restore  # no marker: the driver re-checks the mutant in a fresh process

    marker = Path(path)
    # The file an entry into each watched code object marks, keyed by identity: code objects compare by value. A
    # code object the mutated body shares with the original is the mutant's own, and an entry into it is a touch.
    marks = {id(code): marker for code in codes}
    stale_codes: tuple[CodeType, ...] = ()
    if stale_path is not None:
        stale_codes = tuple(code for code in stale if id(code) not in marks)
        marks.update({id(code): Path(stale_path) for code in stale_codes})
    watched = (*codes, *stale_codes)

    def mark(code: CodeType, _instruction_offset: int) -> kill_deps.MonitoringSignal:
        # Only the watched code objects have the event, and ``watched`` keeps each of them (and so its id) alive.
        with suppress(OSError):  # fail closed: a missing touch marker sends a survivor to a fresh re-check
            marks[id(code)].write_text("1", encoding="utf-8")
        return monitoring.DISABLE

    try:
        monitoring.register_callback(tool_id, monitoring.events.PY_START, mark)
        for code in watched:
            monitoring.set_local_events(tool_id, code, monitoring.events.PY_START)
    except ValueError:
        monitoring.clear_tool_id(tool_id)
        monitoring.free_tool_id(tool_id)
        return _noop_restore

    def disarm() -> None:
        for code in watched:
            monitoring.set_local_events(tool_id, code, 0)
        monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool_id)

    return disarm


def _bytecode_mutant(func: FunctionType, spec: MutantSpec) -> CodeType | None:
    """A surgical edit of the live ``func.__code__`` from the bc-fields; None means fall back to compiling."""
    if os.environ.get(FORCE_COMPILE_ENV):
        return None
    kind_raw = spec.get("bc_kind")
    lineno = spec.get("bc_lineno")
    col = spec.get("bc_col")
    end_col = spec.get("bc_end_col")
    mutated = spec.get("bc_mutated")
    if kind_raw is None or lineno is None or col is None or end_col is None or mutated is None:
        return None
    try:
        edit = BcEdit(
            kind=BcEditKind(kind_raw),
            source=spec.get("bc_source", ""),
            target=spec.get("bc_target", ""),
            mutated=mutated,
        )
    except ValueError:
        return None  # an unknown kind from an untrusted spec: recompile
    return mutate_code(func.__code__, lineno=lineno, col=col, end_col=end_col, edit=edit)


type _Put = Callable[[FunctionType], Revert | None]  # edits one definition; None: the edit does not fit it
type _Watched = Callable[[tuple[FunctionType, ...]], tuple[CodeType, ...]]  # the code the touch marker watches


def apply_mutation(module: ModuleType, spec: MutantSpec) -> ApplyOutcome:
    """``apply_mutation_reversible`` without the revert, for one-shot callers and unit tests."""
    return apply_mutation_reversible(module, spec)[0]


def apply_mutation_reversible(module: ModuleType, spec: MutantSpec) -> tuple[ApplyOutcome, Revert | None]:
    """Prepare a mutant and apply it once: ``(outcome, revert)``; None means nothing was applied."""
    apply = prepare_mutation(module, spec)
    if apply is None:
        return ApplyOutcome.UNSUPPORTED, None
    return ApplyOutcome.APPLIED, apply()


def prepare_mutation(
    module: ModuleType, spec: MutantSpec, *, follow: Callable[[Follower | None], None] | None = None
) -> Apply | None:
    """Resolve the mutant's target and build its edit once; the returned ``apply`` puts the edit into the process
    and returns its revert. None means UNSUPPORTED: no definition, no edit, or an edit that does not fit.

    CODE swaps ``__code__``; DEFAULTS replaces ``__defaults__`` or ``__kwdefaults__``. ``apply`` arms the touch
    marker before it edits anything, so the marker is armed by unmutated code even when the mutant edits this
    module. ``follow`` registers the callback a reload of a module calls during the item: a reload of ``module``
    binds a new definition, which takes the edit at once and again at every later ``apply``."""
    qualname = spec["qualname"]
    func, cache_clear = resolve_with_cache_clear(module, qualname)
    if func is None:
        return None
    edit = _defaults_edit(spec) if spec.get("mechanism") == ApplyMechanism.DEFAULTS.value else _code_edit(func, spec)
    if edit is None:
        return None
    put, watched, originals = edit
    trial = put(func)
    if trial is None:
        return None
    trial()
    targets: list[tuple[FunctionType, Revert]] = [(func, cache_clear)]
    touch_path = spec.get("touch_path")
    stale_path = spec.get("stale_path")

    def apply() -> Revert:
        disarm = _arm_touch_marker(
            watched(tuple(target for target, _clear in targets)), touch_path, stale=originals, stale_path=stale_path
        )
        reverts: list[Revert] = []

        def put_on(target: FunctionType, clear: Revert) -> None:
            # The wrapper cache is empty here: resolving the target cleared it, and so did the previous revert.
            undo = put(target)
            if undo is None:
                return

            def revert_target() -> None:
                undo()
                clear()  # a wrapper cache filled by the mutant must not answer for the original

            reverts.append(revert_target)

        for target, clear in targets:
            put_on(target, clear)

        def carry(reloaded: ModuleType) -> None:
            if reloaded is not module:
                return  # resolving the target would clear its wrapper cache for nothing
            fresh, clear = resolve_with_cache_clear(module, qualname)
            if fresh is None:
                return  # the reloaded body no longer defines the target
            targets.append((fresh, clear))
            put_on(fresh, clear)

        if follow is not None:
            follow(carry)

        def revert() -> None:
            if follow is not None:
                follow(None)
            try:
                for revert_target in reversed(reverts):
                    revert_target()
            finally:
                disarm()

        return revert

    return apply


def _code_edit(func: FunctionType, spec: MutantSpec) -> tuple[_Put, _Watched, tuple[CodeType, ...]] | None:
    """The mutated body for ``__code__``; the touch marker watches the code object that holds the edit, and the
    original code object at the site for an entry that ran the original instead."""
    originals = _codes_at_site(func.__code__, spec.get("target_lineno"), spec.get("target_col"))
    # Domain mutants reference helper names (nullcontext, fuzz/fault helpers, the oracle sentinel): inject them
    # with setdefault so an imported name of the module is never clobbered.
    for name, value in {**fuzz_globals(), **fault_globals(), **oracle_globals()}.items():
        func.__globals__.setdefault(name, value)
    compiled = _bytecode_mutant(func, spec)
    if compiled is None:
        compiled = compile_function_code(spec["mutated_source"], spec["qualname"], spec["module"])
    if compiled is None:
        return None
    new_code: CodeType = compiled
    codes = _codes_at_site(new_code, spec.get("target_lineno"), spec.get("target_col"))

    def put(target: FunctionType) -> Revert | None:
        if target.__code__.co_qualname != new_code.co_qualname:
            # The resolved object is something else (a decorator's wrapper without __wrapped__) than the definition
            # compiled under the spec's qualname: grafting that body would destroy the wrapper.
            return None
        try:
            return patch_code(target, new_code)
        except ValueError:  # freevars/cellvars mismatch: the body changed its closure
            return None

    def watched(_targets: tuple[FunctionType, ...]) -> tuple[CodeType, ...]:
        return codes

    return put, watched, originals


def _defaults_edit(spec: MutantSpec) -> tuple[_Put, _Watched, tuple[CodeType, ...]] | None:
    """One default replaced, positional (``default_index``) or keyword-only (``default_name``); the touch marker
    watches the function's own code. No original code object is watched: the body stays the same.

    Sibling defaults are taken from the already evaluated tuple or mapping (never re-evaluated), so nothing runs as
    a side effect."""
    index = spec.get("default_index")
    name = spec.get("default_name")
    literal = spec.get("default_literal")
    if literal is None:
        return None
    try:
        value = ast.literal_eval(literal)
    except ValueError, SyntaxError:
        return None
    if index is not None:
        put = _positional_default_put(index, value)
    elif name is not None:
        put = _keyword_default_put(name, value)
    else:
        return None

    def watched(targets: tuple[FunctionType, ...]) -> tuple[CodeType, ...]:
        return tuple(target.__code__ for target in targets)

    return put, watched, ()


def _keyword_default_put(name: str, value: object) -> _Put:
    def put(target: FunctionType) -> Revert | None:
        original = target.__kwdefaults__
        if original is None or name not in original:
            return None
        target.__kwdefaults__ = {**original, name: value}

        def undo() -> None:
            target.__kwdefaults__ = original

        return undo

    return put


def _positional_default_put(index: int, value: object) -> _Put:
    def put(target: FunctionType) -> Revert | None:
        original = target.__defaults__
        current: list[object] = list(original or ())
        if not 0 <= index < len(current):
            return None
        current[index] = value
        target.__defaults__ = tuple(current)

        def undo() -> None:
            target.__defaults__ = original

        return undo

    return put


def _mark_unsupported(spec_path: str) -> None:
    """Tell the driver the mutation could not be applied (import / descriptor / compile / freevars)."""
    Path(f"{spec_path}{UNSUPPORTED_MARKER_SUFFIX}").write_text("1", encoding="utf-8")


def _noop_restore() -> None:
    """Nothing to revert: an item of a lease without a mutant, or a touch marker that was not armed."""


def _noop_apply() -> Revert:
    """The apply of a lease without a mutant: calibration, a paired original, or a mutant that is unsupported."""
    return _noop_restore


_OWNED_TRANSPORT_ENV = (
    "PYTEST_FAST_WORKER_IDENTITY",
    "PYTEST_FAST_WORKERS",
    "PYTEST_FAST_MARK",
    "_PYTEST_FAST_COLLECT",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
    "PYTEST_ADDOPTS",
    "COVERAGE_CORE",
)


def scrub_transport_env() -> None:
    """Remove the transport keys before the test body.

    A test that spawns a nested pytest-fast (the coverage tests do) must never inherit this worker's identity,
    addopts or coverage core: the nested run builds its own environment, and an inherited ``PYTEST_XDIST_WORKER``
    makes its plugins skip the per-worker setup and fail closed. The balamut lease keys stay: this plugin reads
    them per item."""
    for key in _OWNED_TRANSPORT_ENV:
        os.environ.pop(key, None)


def pytest_runtest_setup(item: pytest.Item) -> None:
    scrub_transport_env()
    # pytest-fast starts its workers with ``daemon=True``, and multiprocessing refuses to start a child from a
    # daemonic process (a test's ``multiprocessing.Pool``). The flag changes only this process's own view: the
    # controller still terminates the worker as a daemon, and the worker leaves through ``os._exit``.
    multiprocessing.current_process().daemon = False
    _apply_or_switch(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, bool | None, bool | None]:
    """After all phases of a test, revert the mutant it ran against and publish the lease dependencies.

    The revert is written out here rather than called through a helper: this frame started before the mutant was
    applied, so it runs the original code even when the mutant edits this module, while a module-level helper could
    be the mutated one. Not a teardown hook: a failing teardown implementation stops the others, and a killer that
    broke a fixture cleanup would be left without its record. ``finally`` also covers what propagates through the
    protocol."""
    try:
        return (yield)
    finally:
        session = item.session
        revert = session.stash.get(_ITEM, None)
        if revert is not None:
            session.stash[_ITEM] = None
            revert()
        recorder = _deps_recorder(session)
        if recorder is not None:
            recorder.flush()


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup[T](fixturedef: pytest.FixtureDef[T], request: pytest.FixtureRequest) -> Generator[None, T, T]:
    """Building a fixture wider than function scope outlives the lease: its code is a dependency of later kills."""
    recorder = _deps_recorder(request.session)
    if recorder is None or fixturedef.scope == "function":
        return (yield)
    with recorder.shared_scope():
        return (yield)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Arm the daemon's recorder before the root conftest: the application import and collection build state every
    lease reads. Workers inherit the armed recorder through the fork."""
    if not os.environ.get(KILL_DEPS_ENV) or _DEPS in early_config.stash:
        return
    roots = tuple(root for root in os.environ.get(TEST_ROOTS_ENV, "tests").split(",") if root)
    recorder = kill_deps.DepsRecorder(Path.cwd(), test_roots=roots)
    if kill_deps.monitoring_arm(recorder) is not None:  # no free tool id: kills fall back to the file rule
        early_config.stash[_DEPS] = recorder


def pytest_configure(config: pytest.Config) -> None:
    """Route ``importlib.reload`` of a campaign daemon through its reload log (see reloads).

    Installed before collection, so a test module that imports ``reload`` by name binds the hook too. Workers inherit
    the hook and the log through the fork; the cleanup puts the replaced function back."""
    if not os.environ.get(MUTANT_FILE_ENV) or _RELOADS in config.stash:
        return
    replaced = importlib.reload
    log = reload_log(replaced)
    importlib.reload = log.reload
    config.stash[_RELOADS] = log

    def uninstall() -> None:
        if importlib.reload is log.reload:
            importlib.reload = replaced

    config.add_cleanup(uninstall)


def _deps_recorder(session: pytest.Session) -> kill_deps.DepsRecorder | None:
    return session.config.stash.get(_DEPS, None)


def original_lease_deps(raw: str) -> str | None:
    """The dependency file of a paired original: no mutation in its lease, but the green original is the second
    half of the kill proof, so the functions only it enters are dependencies too."""
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    deps_path = parsed.get("original_deps_path")
    return deps_path if isinstance(deps_path, str) and deps_path else None


def _deps_lease(session: pytest.Session, target: str | None) -> None:
    """A lease boundary: the previous mutant's dependencies are dropped, events disabled by DISABLE come back."""
    recorder = _deps_recorder(session)
    if recorder is None:
        return
    recorder.lease_start(Path(target) if target else None)
    kill_deps.monitoring_restart()


def _target_module(item: pytest.Item, spec: MutantSpec, baseline: WorkerBaseline) -> ModuleType | None:
    """The live module to patch, or None (UNSUPPORTED).

    Oracle mutants mutate the test itself: the collected module (``item.module``) is used rather than guessing a
    dotted name, which is fragile for test files outside packages. A source module missing from sys.modules is
    imported lazily by a test body (after its monkeypatching); importing it here, before the test, could bind it
    under the wrong environment. A module present but outside the baseline was imported by a previous mutant's test
    in this persistent worker: in a fresh fork it would not exist, so mutating that state proves nothing."""
    if spec.get("self_covered") is True and isinstance(item, pytest.Function):
        # pytest types `Function.module` as Any; the collected module of a Python test function is a ModuleType.
        return cast("ModuleType", item.module)
    module = sys.modules.get(spec["module"])
    if module is None or spec["module"] not in baseline.modules:
        return None
    return module


def _apply_or_switch(item: pytest.Item) -> None:
    """Apply the mutant of the current lease for this item; at a lease boundary, isolate it from the previous lease.

    The driver puts a unique ``request_token`` into the raw spec. The same raw text means the next item of the same
    lease: the mutant is applied again over the process state the lease's earlier items left. Different text is a
    boundary: the reloads of the previous lease are undone, the safe Python state goes back to the baseline, then
    the new mutant is prepared. The mutant of the previous item is already reverted by ``pytest_runtest_protocol``."""
    spec_path = os.environ.get(MUTANT_FILE_ENV)
    if not spec_path:
        return
    session = item.session
    reloads = session.config.stash.get(_RELOADS, None)
    baseline = session.stash.get(_BASELINE, None)
    if baseline is None:
        # The first mutant of this worker: snapshot the process (after collection) as the reference. A reload run at
        # collection is part of that reference.
        baseline = capture_baseline()
        session.stash[_BASELINE] = baseline
        if reloads is not None:
            reloads.forget()
    lease = session.stash.get(_STATE, None)
    path = Path(spec_path)
    raw: str | None = None
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError, UnicodeDecodeError:  # a directory, binary or unreadable spec: never crash the worker
            _mark_unsupported(spec_path)
    if raw is None:
        # No lease: the tests of this request run against the original.
        _reset(baseline, reloads)
        session.stash[_STATE] = None
        _deps_lease(session, None)
        return
    if lease is not None and lease.key == raw:
        session.stash[_ITEM] = lease.apply()  # the next item of the same lease: no reset, it would clear every cache
        return
    if lease is not None:
        _reset(baseline, reloads)
    lease = _start_lease(item, raw=raw, spec_path=spec_path, baseline=baseline, reloads=reloads)
    session.stash[_STATE] = lease
    session.stash[_ITEM] = lease.apply()


def _reset(baseline: WorkerBaseline, reloads: ReloadLog | None) -> None:
    """A lease boundary: the reloads of the previous lease are undone and the baseline caches cleared."""
    if reloads is not None:
        reloads.undo()
    reset_to_baseline(baseline)


def _start_lease(
    item: pytest.Item, *, raw: str, spec_path: str, baseline: WorkerBaseline, reloads: ReloadLog | None
) -> _Lease:
    """The lease of a new raw spec; an unsupported mutant is marked for the driver and runs as the original.

    Keyword-only: the raw text and the spec path are both strings, and a swap would write the marker under a name
    made of the spec text into the working directory."""
    spec = parse_mutant_spec(raw) if raw else None
    original_deps = original_lease_deps(raw) if raw and spec is None else None
    # The lease starts before the mutation is prepared: the first entry into the mutated code is a dependency too.
    _deps_lease(item.session, spec.get("deps_path") if spec is not None else original_deps)
    if not raw or original_deps is not None:
        # Cost calibration and a paired original carry no mutation; the lease still marks their boundary, so the next
        # mutant lease resets the state those tests polluted before applying.
        return _Lease(raw, _noop_apply)
    module = _target_module(item, spec, baseline) if spec is not None else None
    apply = None
    if spec is not None and module is not None:
        apply = prepare_mutation(module, spec, follow=reloads.follow if reloads is not None else None)
    if apply is None:
        _mark_unsupported(spec_path)
        return _Lease(raw, _noop_apply)
    return _Lease(raw, apply)


__all__ = [
    "FORCE_COMPILE_ENV",
    "KILL_DEPS_ENV",
    "MUTANT_FILE_ENV",
    "STALE_MARKER_SUFFIX",
    "TEST_ROOTS_ENV",
    "TOUCHED_MARKER_SUFFIX",
    "UNSUPPORTED_MARKER_SUFFIX",
    "ApplyOutcome",
    "MutantSite",
    "MutantSpec",
    "apply_mutation",
    "apply_mutation_reversible",
    "parse_mutant_spec",
    "prepare_mutation",
]
