"""Dependencies of a proven kill and their validity.

A kill is an observation: the killer failed on the mutant and passed on the original. It stays true as long as
the mutated function body (part of the mutant key), the code the killing run actually executed and the
test-support corpus are unchanged. The daemon records into the mutant lease the repository functions the run
entered (``sys.monitoring`` PY_START, first entry per code object), including paths taken only by the mutant
and absent from the original's coverage. Hashing whole files instead would rerun almost every mutant on any
edit, although the verdict of a repeat rarely changes.

Validity = the AST hash of the executed functions + the skeleton of their modules (everything but function
bodies: imports, constants, headers, decorators, classes) + the support corpus. In test files the skeleton
leaves test functions out entirely: a new test next to the killer takes no part in its run.

A run also reads what it never enters: constants, registries and configuration of modules loaded at collection
(or by a lazy import of a previous lease), whose bodies ran before the lease. Hence the recorder is armed before
the root conftest, and what ran outside a lease joins the session part of every lease: directories of loaded
modules (their files' skeletons are hashed) and functions called during import. Test modules are covered by the
static import closure of the killing test files: a campaign collects hundreds of files, and an edit of one must
not reset unrelated kills.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import posixpath
import sys
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import CodeType, ModuleType

from balamut.config import Layout
from balamut.coverage.support_deps import imported_modules

type KillDep = tuple[str, str]  # (path relative to the repository root, co_qualname)
type KillDeps = tuple[KillDep, ...]
type _JsonValue = str | int | float | bool | list[_JsonValue] | dict[str, _JsonValue] | None
type _ParsedFiles = dict[str, ast.Module | None]  # None: unreadable or unparseable
type _FileDigests = dict[str, str]
type _SpanDigests = dict[KillDep, str]
type _DirectoryDigests = dict[str, tuple[str, tuple[str, ...]]]  # directory -> (skeleton hash, its files)
type _ImportedTests = dict[str, tuple[str, ...]]
type _LoadedModules = Mapping[str, object]  # sys.modules, whose values are not guaranteed to be modules
# sys.monitoring callbacks return `sys.monitoring.DISABLE`, which typeshed declares as `Final[object]`.
type MonitoringSignal = object

KILL_DEPS_SUFFIX = ".deps"
# 3 and 4 belong to the plugin's touch marker; CPython reserves 5 (optimizer) and 2 (profiler) without using them.
_TOOL_IDS = (5, 2)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_MODULE_BODY = "<module>"
_ANNOTATE = "__annotate__"  # deferred annotations (PEP 649): they evaluate text already in the skeleton
_INSPECT = inspect  # the module object itself: a mutated copy may None the `inspect` name or its attributes
DIRECTORY = "<dir>"  # a directory of modules loaded by the worker: state the process built before the lease
_ENVIRONMENT_PREFIXES = (".venv/", "venv/", ".tox/", ".nox/")


def _test_prefixes(test_roots: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"{root.strip('/')}/" for root in test_roots if root not in {".", ""})


def kill_deps_encode(deps: KillDeps) -> str:
    """Serialize dependencies for the store: sorted unique pairs."""
    return json.dumps(sorted(set(deps)), ensure_ascii=False, separators=(",", ":"))


def kill_deps_decode(raw: str) -> KillDeps | None:
    """Parse stored dependencies; None when empty or corrupt (they cannot prove a kill)."""
    try:
        parsed: _JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    deps: list[KillDep] = []
    for entry in parsed:
        if not isinstance(entry, list) or len(entry) != 2:
            return None
        path, qualname = entry
        if not isinstance(path, str) or not isinstance(qualname, str):
            return None
        deps.append((path, qualname))
    return tuple(sorted(set(deps)))


def kill_deps_append(path: Path, deps: Iterable[KillDep]) -> None:
    """Append one line of lease dependencies. The driver reads the file after the request completed, when the
    worker has written everything; a process that died mid-write is visible to the driver, which then ignores it."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(kill_deps_encode(tuple(deps)) + "\n")


def kill_deps_read(path: Path) -> KillDeps | None:
    """Dependencies recorded for a lease; None when the file is missing, empty or has any corrupt line."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    deps: set[KillDep] = set()
    for line in lines:
        decoded = kill_deps_decode(line)
        if decoded is None:
            return None
        deps.update(decoded)
    return tuple(sorted(deps)) if deps else None


class DepsRecorder:
    """Repository functions the current mutant lease entered, plus state built outside the lease.

    The session part is carried into every lease, otherwise it would drop out of the dependencies of every kill
    but the first: directories of loaded modules, functions run before the first lease (import, collection) or
    by the body of a lazily imported module, and fixtures wider than function scope, whose results every later
    lease of the worker uses."""

    def __init__(
        self, root: Path, *, test_roots: Iterable[str] = ("tests",), modules: _LoadedModules = sys.modules
    ) -> None:
        super().__init__()
        self._roots = tuple(dict.fromkeys((f"{root}{os.sep}", f"{root.resolve()}{os.sep}")))
        self._test_prefixes = _test_prefixes(test_roots)
        self._lease: set[KillDep] = set()
        self._shared: set[KillDep] = set()
        self._published: set[KillDep] = set()  # already in the lease file: the next flush appends only what is new
        self._shared_depth = 0
        self._target: Path | None = None
        self._leased = False  # import and collection happen before the first lease
        self._modules = modules
        self._modules_seen = -1

    def _relative(self, filename: str) -> str | None:
        """Path relative to the repository root; None outside it or inside a virtual environment."""
        root = next((candidate for candidate in self._roots if filename.startswith(candidate)), None)
        if root is None:
            return None
        relative = filename[len(root) :].replace(os.sep, "/")
        if relative.startswith(_ENVIRONMENT_PREFIXES) or "/site-packages/" in relative:
            return None
        return relative

    def _is_test(self, relative: str) -> bool:
        return relative.startswith(self._test_prefixes)

    def record(self, filename: str, qualname: str) -> None:
        """Note an entry into a code object; anything outside the repository or in an environment is skipped.

        An empty definition path is a module body or a module-level expression (lambda, genexpr, generic
        parameters): its text is the file skeleton, carried by the module's directory (or, for a test module, by
        the import closure). Functions a test module's import called are not recorded either: the closure holds
        those bodies in full."""
        relative = self._relative(filename)
        if relative is None or not _definition_path(qualname):
            return
        dep = (relative, qualname)
        if not self._leased or self._module_body_running():
            if not self._is_test(relative):
                self._shared.add(dep)
            return
        self._lease.add(dep)
        if self._shared_depth:
            self._shared.add(dep)

    def _module_body_running(self) -> bool:
        """Is a repository module body on the stack (a lazy import)? What it calls builds state outliving the lease."""
        frame = inspect.currentframe()
        while frame is not None:
            code = frame.f_code
            if (
                code.co_name == _MODULE_BODY
                and frame.f_globals.get("__name__") != "__main__"
                and self._relative(code.co_filename) is not None
            ):
                return True
            frame = frame.f_back
        return False

    def lease_start(self, target: Path | None) -> None:
        """Start a lease: the previous mutant's dependencies are dropped, the session part is kept."""
        self._lease = set()
        self._published = set()
        self._target = target
        self._leased = True

    @contextmanager
    def shared_scope(self) -> Generator[None]:
        """Code run inside outlives the lease (building a fixture wider than function scope)."""
        self._shared_depth += 1
        try:
            yield
        finally:
            self._shared_depth -= 1

    def flush(self) -> None:
        """Append what the lease has not published yet; a lease without a target writes nothing."""
        if self._target is None:
            return
        self._modules_scan()
        fresh = (self._lease | self._shared) - self._published
        if not fresh:
            return
        kill_deps_append(self._target, fresh)
        self._published |= fresh

    def _modules_scan(self) -> None:
        """Directories of loaded repository modules join the session part; rescanned only when something loaded."""
        modules = self._modules
        if len(modules) == self._modules_seen:
            return
        self._modules_seen = len(modules)
        for module in list(modules.values()):
            spec = module.__spec__ if isinstance(module, ModuleType) else None
            if spec is None or not spec.has_location or spec.origin is None:
                continue
            relative = self._relative(spec.origin)
            if relative is not None and not self._is_test(relative):
                self._shared.add((posixpath.dirname(relative) or ".", DIRECTORY))


def monitoring_arm(recorder: DepsRecorder, *, tool_ids: tuple[int, ...] = _TOOL_IDS) -> Callable[[], None] | None:
    """Subscribe the recorder to the first entry into every code object; None when no tool id is free.

    ``DISABLE`` after the first event keeps monitoring almost free: each code object reports once until
    ``monitoring_restart`` starts the next lease."""
    monitoring = sys.monitoring
    tool_id = next((candidate for candidate in tool_ids if _tool_id_take(candidate)), None)
    if tool_id is None:
        return None

    def on_start(code: CodeType | None, _offset: int) -> MonitoringSignal:
        # The interpreter delivers a bare None event during shutdown (an object finalized with a live callback);
        # recording it would crash the unraisable hook. A mutated copy of this module can None any name or
        # attribute this body touches; the damage must end here, not in the unraisable hook.
        if code is None:
            return monitoring.DISABLE
        try:
            optimized = _INSPECT.__dict__.get("CO_OPTIMIZED")
        except AttributeError:
            return monitoring.DISABLE
        if not isinstance(optimized, int):
            return monitoring.DISABLE
        # A class body (no CO_OPTIMIZED) and deferred annotations run skeleton text; recorded as a function, a
        # class body would absorb the bodies of all its methods into the hash.
        if code.co_flags & optimized and code.co_name != _ANNOTATE:
            recorder.record(code.co_filename, code.co_qualname)
        return monitoring.DISABLE

    monitoring.register_callback(tool_id, monitoring.events.PY_START, on_start)
    monitoring.set_events(tool_id, monitoring.events.PY_START)

    def disarm() -> None:
        monitoring.set_events(tool_id, 0)
        monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool_id)

    return disarm


def _tool_id_take(tool_id: int) -> bool:
    try:
        sys.monitoring.use_tool_id(tool_id, "balamut-kill-deps")
    except ValueError:  # taken by another tool of this process
        return False
    return True


def monitoring_restart() -> None:
    """Re-enable events switched off by ``DISABLE``: a new lease must see already known functions too."""
    sys.monitoring.restart_events()


class DependencyHasher:
    """Hashes of executed functions and their module skeletons in the *current* tree; each file parsed once."""

    def __init__(self, layout: Layout) -> None:
        super().__init__()
        self._root = layout.root
        self._layout = layout
        self._test_prefixes = _test_prefixes(layout.tests)
        self._trees: _ParsedFiles = {}
        self._skeletons: _FileDigests = {}
        self._states: _FileDigests = {}
        self._spans: _SpanDigests = {}
        self._directories: _DirectoryDigests = {}
        self._imported: _ImportedTests = {}

    def _is_test(self, relative: str) -> bool:
        return relative.startswith(self._test_prefixes)

    def validity(self, deps: KillDeps, *, support_hash: str) -> str:
        functions = sorted({dep for dep in deps if dep[1] != DIRECTORY})
        parts = [support_hash]
        parts.extend(
            f"file\0{file}\0{self._skeleton(file)}"
            for file in sorted({file for file, _ in functions if not self._is_test(file)})
        )
        parts.extend(f"module\0{file}\0{self._state(file)}" for file in self._test_closure(functions))
        parts.extend(f"dir\0{directory}\0{self._directory(directory)[0]}" for directory in _directories(deps))
        parts.extend(f"span\0{file}\0{qualname}\0{self._span((file, qualname))}" for file, qualname in functions)
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def files(self, deps: KillDeps) -> set[str]:
        """Files the validity of ``deps`` is built from, relative to the root."""
        functions = [dep for dep in deps if dep[1] != DIRECTORY]
        files = {file for file, _ in functions}
        files.update(self._test_closure(functions))
        for directory in _directories(deps):
            files.update(self._directory(directory)[1])
        return files

    def _tree(self, file: str) -> ast.Module | None:
        if file not in self._trees:
            self._trees[file] = _parse(self._root / file)
        return self._trees[file]

    def _skeleton(self, file: str) -> str:
        if file not in self._skeletons:
            # The skeleton transform edits a tree in place: a tree cached for function hashes is copied first.
            cached = self._trees.get(file)
            tree = _copy(cached) if cached is not None else _parse(self._root / file)
            drop_tests = self._layout.is_test_file(Path(file).name)
            self._skeletons[file] = (
                "unreadable" if tree is None else _digest(_Skeleton(drop_tests=drop_tests).visit(tree))
            )
        return self._skeletons[file]

    def _state(self, file: str) -> str:
        """A test module in full except its tests: the bodies of its other functions run on import too (parameter
        building, module constants), and the recorder does not record what a test module's import called."""
        if file not in self._states:
            tree = self._tree(file)
            self._states[file] = (
                "unreadable"
                if tree is None
                else _digest(_Skeleton(drop_tests=True, keep_bodies=True).visit(_copy(tree)))
            )
        return self._states[file]

    def _directory(self, directory: str) -> tuple[str, tuple[str, ...]]:
        """Skeletons of every module of a directory, including one that appeared later: importing the package may
        have added it to a registry."""
        if directory not in self._directories:
            base = self._root / directory
            files = (
                tuple(sorted(path.relative_to(self._root).as_posix() for path in base.glob("*.py")))
                if base.is_dir()
                else ()
            )
            listing = "\n".join(f"{file}\0{self._skeleton(file)}" for file in files)
            self._directories[directory] = (hashlib.sha256(listing.encode("utf-8")).hexdigest(), files)
        return self._directories[directory]

    def _test_closure(self, functions: Iterable[KillDep]) -> list[str]:
        """Test modules of the kill and everything they import from the test tree: constants, helpers and
        parameters the run reads without entering any of their functions."""
        pending = sorted({file for file, _ in functions if self._is_test(file)})
        seen = set(pending)
        while pending:
            for imported in self._imported_tests(pending.pop()):
                if imported not in seen:
                    seen.add(imported)
                    pending.append(imported)
        return sorted(seen)

    def _imported_tests(self, file: str) -> tuple[str, ...]:
        if file not in self._imported:
            tree = self._tree(file)
            modules = (module for node in ast.walk(tree) for module in imported_modules(node, file)) if tree else ()
            self._imported[file] = tuple(sorted({found for module in modules for found in self._module_files(module)}))
        return self._imported[file]

    def _module_files(self, module: str) -> Iterator[str]:
        """The test-tree file of a module (a module or a package), if it exists."""
        relative = module.replace(".", "/")
        if not self._is_test(f"{relative}/"):
            return
        for candidate in (f"{relative}.py", f"{relative}/__init__.py"):
            if (self._root / candidate).is_file():
                yield candidate

    def _span(self, dep: KillDep) -> str:
        if dep not in self._spans:
            file, qualname = dep
            tree = self._tree(file)
            if tree is None:
                self._spans[dep] = "unreadable"
            else:
                path = _definition_path(qualname)
                nodes = _definitions(tree.body, path) if path else ()
                # An empty path is a module-level expression (lambda, genexpr): its text is in the skeleton already.
                self._spans[dep] = "module-scope" if not path else ("absent" if not nodes else _digest(*nodes))
        return self._spans[dep]

    def warm(self, files: Iterable[str]) -> None:
        """Parse the given files up front, in parallel; the serial first-touch parse dominates the cached prep."""
        pending = sorted(file for file in files if file not in self._trees)
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as executor:
            executor.map(self._tree, pending)

    def layout_files(self) -> list[str]:
        """Every source file of the layout, relative to the root (the skeletons and spans come from them)."""
        candidates: list[str] = []
        for root_name in self._layout.source:
            base = self._root / root_name
            if base.is_dir():
                candidates.extend(path.relative_to(self._root).as_posix() for path in base.rglob("*.py"))
        return candidates


def _directories(deps: KillDeps) -> list[str]:
    return sorted({file for file, qualname in deps if qualname == DIRECTORY})


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, SyntaxError, ValueError:
        return None


class KillValidity:
    """Kill validity for one campaign: one shared parse of files for all worker threads.

    ``since`` is the campaign start (for recording observed kills). The daemon executes the file version
    imported at start while the hash reads the current one: a dependency changed later yields ``""`` and the
    kill falls back to the file rule."""

    def __init__(self, layout: Layout, *, support_hash: str, since: float | None = None) -> None:
        super().__init__()
        self._root = layout.root
        self._hasher = DependencyHasher(layout)
        self._support_hash = support_hash
        self._since = since
        self._lock = threading.Lock()

    def warm_layout(self) -> None:
        """Parse every source and test file of the layout once, in parallel (the campaign's prep warms this)."""
        self._hasher.warm(self._hasher.layout_files())

    def of(self, deps: KillDeps) -> str:
        """Validity of a kill just observed, for recording."""
        with self._lock:
            validity = self._hasher.validity(deps, support_hash=self._support_hash)
            files = self._hasher.files(deps)
        since = self._since
        # Checked after reading: passing means what was read is the version from campaign start.
        if since is not None and any(_changed_since(self._root / file, since) for file in files):
            return ""
        return validity

    def current(self, deps: KillDeps) -> str:
        """Validity in the current tree, for comparison with the recorded one."""
        with self._lock:
            return self._hasher.validity(deps, support_hash=self._support_hash)


def _changed_since(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime > since
    except OSError:  # the file is gone: whatever it was when observed, the hash no longer describes it
        return True


def _digest(*nodes: ast.AST) -> str:
    dumped = "\n".join(ast.dump(node, include_attributes=False) for node in nodes)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _copy(tree: ast.Module) -> ast.Module:
    """A separate tree for the skeleton transform, so the tree cached for function hashes stays intact."""
    return ast.parse(ast.unparse(tree))


def _definition_path(qualname: str) -> tuple[str, ...]:
    """The definition whose text covers a code object: a nested function lives in the outer function's body."""
    parts: list[str] = []
    for part in qualname.split("."):
        if part.startswith("<"):
            break
        parts.append(part)
    return tuple(parts)


def _scope_statements(nodes: Iterable[ast.AST]) -> Iterator[ast.stmt]:
    """Statements of one scope, including those nested in if/try/with/for/match, not in def/class bodies."""
    for node in nodes:
        if isinstance(node, ast.stmt):
            yield node
        if isinstance(node, (*_FUNCTION_NODES, ast.ClassDef)):
            continue
        yield from _scope_statements(ast.iter_child_nodes(node))


def _definitions(body: list[ast.stmt], path: tuple[str, ...]) -> tuple[ast.AST, ...]:
    """All definitions along a name path: same-named ones (property getter/setter, overload) count together."""
    head, rest = path[0], path[1:]
    matches = [
        node
        for node in _scope_statements(body)
        if isinstance(node, (*_FUNCTION_NODES, ast.ClassDef)) and node.name == head
    ]
    if not rest:
        return tuple(matches)
    return tuple(found for node in matches if isinstance(node, ast.ClassDef) for found in _definitions(node.body, rest))


class _Skeleton(ast.NodeTransformer):
    """A module skeleton: function bodies are replaced; headers, decorators and module/class level stay.

    ``keep_bodies`` keeps the bodies of non-test functions, so a test module also describes what its import ran."""

    def __init__(self, *, drop_tests: bool, keep_bodies: bool = False) -> None:
        super().__init__()
        self._drop_tests = drop_tests
        self._keep_bodies = keep_bodies
        self._classes: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self._classes.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._classes.pop()
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef | None:
        return self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef | None:
        return self._function(node)

    def _function[F: (ast.FunctionDef, ast.AsyncFunctionDef)](self, node: F) -> F | None:
        if self._is_test(node.name):
            return None
        if not self._keep_bodies:
            node.body = [ast.Pass()]
        return node

    def _is_test(self, name: str) -> bool:
        """A test function takes part in the killer's run only if it is the killer (then it is a dependency)."""
        if not self._drop_tests or not name.startswith("test"):
            return False
        return not self._classes or self._classes[-1].startswith("Test")
