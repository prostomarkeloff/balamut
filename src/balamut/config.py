"""Project profile: everything balamut needs to know about the project it mutates.

The profile comes from one Python config file: ``[tool.balamut]`` in ``pyproject.toml`` names it
(``config = "balamut_conf.py"``); everything else lives in that file. It may define
``profile(root) -> Profile``, optionally ``KINDS`` (a ``name -> OperatorKind`` registry importing and
overriding kinds) and ``REMOVE_KINDS`` (built-in kinds to opt out of). Without a config
file the conventional ``src/`` + ``tests/`` defaults apply. A malformed profile raises
``ProfileError``; unknown keys have nothing to hide in (there is no key table anymore) — a typo
is a Python ``NameError``/``TypeError`` at load time.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import cast

from balamut.operators.facts import OperatorKind

type SemanticPatterns = tuple[tuple[str, tuple[str, ...]], ...]
type StringMap = dict[str, str]
type ConfigCache = dict[tuple[str, int], ModuleType]
type KindRegistry = dict[str, OperatorKind]

SINK_ENTRY_POINT_GROUP = "balamut.sinks"

# The built-in operator kinds, in their default order. The engine loads them lazily; the config
# file's KINDS registers or overrides any of them (and custom ones).
BUILTIN_KINDS: tuple[str, ...] = ("src", "concurrency", "fault", "oracle", "extreme", "db", "chain")

DEFAULT_KINDS = ("src", "concurrency", "fault", "oracle", "extreme")
DEFAULT_DIFF_KINDS = DEFAULT_KINDS
DEFAULT_TEST_PATTERNS = ("test_*.py", "*_test.py")
DEFAULT_SUBSTRATE_INPUTS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py")
DEFAULT_SEMANTIC: SemanticPatterns = (
    ("tenant", ("tenant_id",)),
    ("auth", ("status_code=401", "status_code=403")),
    ("time", ("expires_at", "utcnow", "timedelta")),
    ("transaction", (".commit(", ".flush(", ".rollback(")),
    ("pagination", (".limit(", ".offset(", "cursor")),
)

_CONFIG_CACHE: ConfigCache = {}


class ProfileError(ValueError):
    """The project config is malformed; the message names the offending value."""


class IsolationKind(Enum):
    NONE = "none"
    SHELL = "shell"
    POSTGRES_TEMPLATE = "postgres-template"


@dataclass(frozen=True, slots=True)
class Layout:
    """Where source and tests live and how a source path maps to an importable module name."""

    root: Path
    source: tuple[str, ...]
    import_roots: tuple[str, ...]
    tests: tuple[str, ...]
    test_patterns: tuple[str, ...]

    def is_test_file(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.test_patterns)

    def is_source_path(self, rel: str) -> bool:
        return rel.endswith(".py") and any(_under(rel, root) for root in self.source) and not self.is_test_path(rel)

    def is_test_path(self, rel: str) -> bool:
        return any(_under(rel, root) for root in self.tests)

    def module_name(self, rel: str) -> str:
        """``src/pkg/mod.py`` → ``pkg.mod``; a package ``__init__`` maps to the package itself.

        The package must be addressed as ``pkg`` rather than ``pkg.__init__``: importlib would load the latter
        as a separate module object, and a patch applied to that copy is invisible to the code under test."""
        roots = sorted((root for root in self.import_roots if root == "." or _under(rel, root)), key=len, reverse=True)
        stripped = rel
        for root in roots:
            if root != ".":
                stripped = rel[len(root.rstrip("/")) + 1 :]
            break
        module = stripped.removesuffix(".py").replace("/", ".")
        return module.removesuffix(".__init__")

    def watch_dirs(self) -> tuple[str, ...]:
        """Directories whose ``*.py`` edits must make a warm pytest-fast daemon stale."""
        dirs = [root for root in (*self.source, *self.tests) if root not in {".", ""}]
        return tuple(dict.fromkeys(dirs))


def _under(rel: str, root: str) -> bool:
    if root in {".", ""}:
        return True
    trimmed = root.rstrip("/")
    return rel == trimmed or rel.startswith(trimmed + "/")


@dataclass(frozen=True, slots=True)
class CoverageSettings:
    concurrency: tuple[str, ...] = ("thread",)
    workers_cap: int = 4


@dataclass(frozen=True, slots=True)
class DatabaseEnv:
    """Names of the environment variables the project reads its database connection from."""

    host: str
    port: str
    database: str
    user: str
    password: str


@dataclass(frozen=True, slots=True)
class IsolationSettings:
    kind: IsolationKind = IsolationKind.NONE
    env: DatabaseEnv | None = None
    migrate: tuple[str, ...] = ()
    migrate_env: tuple[tuple[str, str], ...] = ()
    schema_inputs: tuple[str, ...] = ()
    prepare: tuple[str, ...] = ()
    cleanup: tuple[str, ...] = ()
    locale: str | None = None  # LC_COLLATE/LC_CTYPE of the schema template; None keeps the server default


@dataclass(frozen=True, slots=True)
class QueueFamily:
    """A queue table whose claim/settle predicates get queue-semantics concurrency mutants.

    ``fence`` columns compared with ``==`` against a non-literal (a fencing token) lose the fence; ``due``
    (``<=``), ``lease`` (``<``), ``backstop`` (``<``) and ``progress`` (``>``) predicates collapse to a
    tautology."""

    model: str
    family: str
    fence: tuple[str, ...] = ()
    due: str | None = None
    lease: str | None = None
    backstop: str | None = None
    progress: str | None = None


@dataclass(frozen=True, slots=True)
class Accept:
    """A mutation the project accepts unkilled: a reviewed decision that no test is owed for it.

    The gate reports an accepted mutant apart and never fails on it: an equivalent mutant (no test can tell it
    from the original) or an optimisation whose removal changes no result. ``reason`` is mandatory and says why.
    Every other field narrows the rule, and an empty one matches anything: ``path`` is the file relative to the
    project root, ``function`` the qualified name of the enclosing function (``Class.method``), ``operator`` an
    operator name or a prefix of one, ``original`` the mutated source text as a report prints it (runs of
    whitespace compare as one space) or a compiled pattern that text must match in full."""

    reason: str
    path: str = ""
    function: str = ""
    operator: str = ""
    original: str | re.Pattern[str] = ""


@dataclass(frozen=True, slots=True)
class OperatorSettings:
    # The cross-kind survivor tagging policy: class -> source tokens. Kind data lives in the config file's
    # KINDS registry (facts are constructed there, with their data), not here.
    semantic: SemanticPatterns = DEFAULT_SEMANTIC


@dataclass(frozen=True, slots=True)
class Profile:
    layout: Layout
    base: str = "origin/main"
    kinds: tuple[str, ...] = DEFAULT_KINDS
    diff_kinds: tuple[str, ...] = DEFAULT_DIFF_KINDS
    schedule_fuzz: bool = True
    env_prefixes: tuple[str, ...] = ()
    substrate_inputs: tuple[str, ...] = DEFAULT_SUBSTRATE_INPUTS
    mutant_timeout: float = 8.0
    timeout_retries: int = 1
    # Seconds a campaign daemon idles before it exits cleanly; None keeps the engine's built-in TTL (600 s).
    daemon_ttl: int | None = None
    state_dir: Path = field(default_factory=lambda: Path(".balamut"))
    preflight: tuple[tuple[str, ...], ...] = ()
    coverage: CoverageSettings = field(default_factory=CoverageSettings)
    isolation: IsolationSettings = field(default_factory=IsolationSettings)
    operators: OperatorSettings = field(default_factory=OperatorSettings)
    accept: tuple[Accept, ...] = ()

    @property
    def root(self) -> Path:
        return self.layout.root


def _empty_kinds() -> KindRegistry:
    return {}


@dataclass(frozen=True, slots=True)
class KindOverrides:
    """The config file's kind registry: built-in overrides, custom registrations and opt-outs."""

    kinds: KindRegistry = field(default_factory=_empty_kinds)
    removed: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    """What one config file declares: the profile plus the kind registry."""

    profile: Profile
    overrides: KindOverrides


def init(
    *,
    source: tuple[str, ...] = ("src",),
    import_roots: tuple[str, ...] | None = None,
    tests: tuple[str, ...] = ("tests",),
    test_patterns: tuple[str, ...] = DEFAULT_TEST_PATTERNS,
    state_dir: str = ".balamut",
    base: str = "origin/main",
    kinds: tuple[str, ...] = DEFAULT_KINDS,
    diff_kinds: tuple[str, ...] = DEFAULT_DIFF_KINDS,
    schedule_fuzz: bool = True,
    env_prefixes: tuple[str, ...] = (),
    substrate_inputs: tuple[str, ...] = DEFAULT_SUBSTRATE_INPUTS,
    mutant_timeout: float = 8.0,
    timeout_retries: int = 1,
    daemon_ttl: int | None = None,
    preflight: tuple[tuple[str, ...], ...] = (),
    coverage_concurrency: tuple[str, ...] = ("thread",),
    coverage_workers_cap: int = 4,
    isolation: IsolationSettings | None = None,
    semantic: SemanticPatterns | None = None,
    register: KindRegistry | None = None,
    remove: frozenset[str] | None = None,
    accept: tuple[Accept, ...] = (),
) -> None:
    """The config file's one call: declare the whole project.

    ``source``/``tests``/``state_dir`` are relative to the project root; ``import_roots`` defaults
    to ``("src",)`` when a ``src`` directory exists, else ``("." ,)``. ``register`` maps kind names
    to their tuples of facts (import them right here); ``remove`` opts out of built-in kinds;
    ``accept`` lists the mutations the gate never fails on, each with its reason."""
    resolved = _PENDING_ROOT.get("root")
    if resolved is None:
        raise ProfileError("init(...) may only be called from the config file")
    if import_roots is None:
        import_roots = ("src",) if (resolved / "src").is_dir() else (".",)
    profile = Profile(
        layout=Layout(
            root=resolved,
            source=source,
            import_roots=import_roots,
            tests=tests,
            test_patterns=test_patterns,
        ),
        state_dir=resolved / state_dir,
        base=base,
        kinds=kinds,
        diff_kinds=diff_kinds,
        schedule_fuzz=schedule_fuzz,
        env_prefixes=env_prefixes,
        substrate_inputs=substrate_inputs,
        mutant_timeout=mutant_timeout,
        timeout_retries=timeout_retries,
        daemon_ttl=daemon_ttl,
        preflight=preflight,
        coverage=CoverageSettings(concurrency=coverage_concurrency, workers_cap=coverage_workers_cap),
        isolation=isolation if isolation is not None else IsolationSettings(),
        operators=OperatorSettings(semantic=semantic if semantic is not None else DEFAULT_SEMANTIC),
        accept=accept,
    )
    registry = _registry(register)
    _validate_profile(profile, registered=frozenset(registry))
    _PENDING_SPEC["spec"] = ProjectSpec(
        profile=profile,
        overrides=KindOverrides(kinds=registry, removed=remove if remove is not None else frozenset()),
    )


def _registry(register: KindRegistry | None) -> KindRegistry:
    """The ``register`` argument as-is: resolve_kinds re-validates each kind's facts."""
    return register if register is not None else {}


type PendingRoot = dict[str, Path]
type PendingSpec = dict[str, ProjectSpec]
type SpecCache = dict[tuple[str, int], ProjectSpec]

_PENDING_ROOT: PendingRoot = {}
_PENDING_SPEC: PendingSpec = {}
_SPEC_CACHE: SpecCache = {}


def default_profile(root: Path) -> Profile:
    resolved = root.resolve()
    profile = Profile(
        layout=Layout(
            root=resolved,
            source=("src",),
            import_roots=("src",) if (resolved / "src").is_dir() else (".",),
            tests=("tests",),
            test_patterns=DEFAULT_TEST_PATTERNS,
        ),
        state_dir=resolved / ".balamut",
    )
    _validate_profile(profile)
    return profile


def load_spec(root: Path) -> ProjectSpec:
    """The project config: the file named by ``[tool.balamut]`` (which must call ``init``), or the
    conventional defaults when neither is present."""
    resolved = root.resolve()
    config_path = _config_path(resolved)
    if config_path is None:
        return ProjectSpec(profile=default_profile(resolved), overrides=KindOverrides())
    key = (str(resolved), config_path.stat().st_mtime_ns)
    cached = _SPEC_CACHE.get(key)
    if cached is not None:
        return cached
    _PENDING_ROOT["root"] = resolved
    _PENDING_SPEC.pop("spec", None)
    try:
        _load_config_module(config_path)
    finally:
        _PENDING_ROOT.pop("root", None)
    spec = _PENDING_SPEC.get("spec")
    if spec is None:
        raise ProfileError(f"{config_path.name}: must call init(...)")
    _SPEC_CACHE[key] = spec
    return spec


def load_profile(root: Path) -> Profile:
    """The project profile (the config file's ``init`` arguments, or the defaults)."""
    return load_spec(root).profile


def load_kind_overrides(root: Path) -> KindOverrides:
    """The config file's kind registry and opt-outs (empty without a config file)."""
    return load_spec(root).overrides


def _config_path(root: Path) -> Path | None:
    """The config file named by ``[tool.balamut]`` (the only key of that table)."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"pyproject.toml: {exc}") from exc
    tool = document.get("tool")
    if not isinstance(tool, dict):
        return None
    # TOML documents are dict[str, Any]; after the isinstance guard the shape is fixed.
    tool_table = cast("dict[str, object]", tool)
    raw = tool_table.get("balamut")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError("tool.balamut must be a table")
    # TOML documents are dict[str, Any]; after the isinstance guard the shape is fixed.
    table = cast("dict[str, object]", raw)
    if not all(key == "config" for key in table):
        raise ProfileError("tool.balamut: only `config` belongs here; the profile lives in the Python config file")
    name = table.get("config")
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise ProfileError("tool.balamut.config must name the config file")
    path = root / name
    if not path.is_file():
        raise ProfileError(f"tool.balamut.config: {name} does not exist")
    return path


def _load_config_module(path: Path) -> ModuleType:
    """Import the config file as a module, cached by path and mtime (tests rewrite projects in-process)."""
    resolved = path.resolve()
    key = (str(resolved), resolved.stat().st_mtime_ns)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached
    module_name = f"_balamut_config_{abs(hash(str(resolved))) % (10**9)}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ProfileError(f"{path.name}: cannot be imported as a module")
    module = importlib.util.module_from_spec(spec)
    sys_path_added = False
    if str(resolved.parent) not in sys.path:
        sys.path.insert(0, str(resolved.parent))
        sys_path_added = True
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ProfileError(f"{path.name}: failed to import: {exc}") from exc
    finally:
        if sys_path_added:
            sys.path.remove(str(resolved.parent))
    sys.modules[module_name] = module
    _CONFIG_CACHE[key] = module
    return module


def _validate_profile(profile: Profile, *, registered: frozenset[str] = frozenset()) -> None:
    """The cross-field invariants, now on the typed value itself.

    ``registered`` are the kind names the config file registers (its KINDS keys): the profile may
    name them alongside the built-ins."""
    known = frozenset(BUILTIN_KINDS) | registered
    unknown_kinds = sorted(set(profile.kinds) - known)
    if unknown_kinds:
        raise ProfileError(f"kinds: unknown kind(s) {', '.join(unknown_kinds)}; known: {', '.join(sorted(known))}")
    unknown_diff = sorted(set(profile.diff_kinds) - known)
    if unknown_diff:
        raise ProfileError(f"diff_kinds: unknown kind(s) {', '.join(unknown_diff)}; known: {', '.join(sorted(known))}")
    if profile.mutant_timeout <= 0:
        raise ProfileError("mutant_timeout must be positive")
    if profile.timeout_retries < 0:
        raise ProfileError("timeout_retries must be >= 0")
    if profile.daemon_ttl is not None and profile.daemon_ttl <= 0:
        raise ProfileError("daemon_ttl must be positive")
    if profile.coverage.workers_cap < 1:
        raise ProfileError("coverage.workers_cap must be >= 1")
    isolation = profile.isolation
    if isolation.kind is IsolationKind.POSTGRES_TEMPLATE and isolation.env is None:
        raise ProfileError("isolation.env is required for postgres-template (names of the DB_* variables)")
    if isolation.kind is IsolationKind.SHELL and not isolation.prepare:
        raise ProfileError("isolation.prepare is required for shell isolation")
    for index, rule in enumerate(profile.accept):
        if not rule.reason.strip():
            raise ProfileError(f"accept[{index}]: a reason is required")
        if not (rule.path or rule.function or rule.operator or rule.original):
            raise ProfileError(f"accept[{index}]: name a path, a function, an operator or an original text")
