# Configuration

balamut reads one Python config file per project: `[tool.balamut]` in the `pyproject.toml` of the project root
names it (`config = "balamut_conf.py"`); everything else lives in that file. The root is the nearest directory
whose `pyproject.toml` has the table (otherwise the nearest with a `pyproject.toml`; `balamut --root DIR`
overrides it). Without a config file the conventional `src/` + `tests/` defaults apply.

The file calls `init(**kwargs)` once, declaring the whole project — layout, campaign, isolation and the kind
registry (see below). Everything is typed: a wrong field or a wrong kind name fails at load with `ProfileError`.

```python
# balamut_conf.py
from balamut.config import init

init()
```

## Layout

The layout is declared through the `source`, `import_roots`, `tests`, `test_patterns` and `state_dir` arguments
of `init`.

| argument | default | meaning |
|---|---|---|
| `source` | `("src",)` | source roots: the files a campaign mutates and coverage measures |
| `import_roots` | `("src",)` when it exists, else `(".",)` | roots a source path is imported from (`src/pkg/mod.py` is `pkg.mod`) |
| `tests` | `("tests",)` | test roots |
| `test_patterns` | `("test_*.py", "*_test.py")` | file names pytest collects |
| `state_dir` | `".balamut"` | where the substrate, the verdict cache, scratch files and reports live; it ignores itself in git |

## Campaign

`init` arguments (all optional, defaults in brackets):

| argument | default | meaning |
|---|---|---|
| `base` | `"origin/main"` | the ref `balamut diff` takes the diff against (through the merge base) |
| `kinds` | `("src", "concurrency", "fault", "oracle", "extreme")` | mutant kinds of `balamut mutate` |
| `diff_kinds` | `("src", "concurrency", "fault", "oracle", "extreme")` | mutant kinds of `balamut diff` |
| `schedule_fuzz` | `True` | inject yield points into concurrency mutants so a race has a chance to show |
| `mutant_timeout` | `8.0` | no-progress timeout of a mutant request, in seconds (raised to twice the slowest covering test) |
| `daemon_ttl` | `None` (600 s) | seconds a campaign daemon may idle before it exits cleanly; a lane respawns such a daemon instead of failing |
| `timeout_retries` | `1` | isolated retries of a timed-out mutant, each with a three times longer budget |
| `env_prefixes` | `()` | prefixes of application environment variables a warm daemon must restart on (shared environments only) |
| `preflight` | `()` | commands `balamut diff` and `balamut doctor` run first, as argv arrays: `(("make", "db-ready"),)` |
| `substrate_inputs` | `("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py")` | files whose change rebuilds the substrate |
| `coverage_concurrency` | `("thread",)` | coverage.py concurrency of the measurement |
| `coverage_workers_cap` | `4` | parallel workers of a substrate build |
| `semantic` | (the defaults) | survivor classes by the text of the mutated code (see below) |
| `accept` | `()` | mutants the gate never fails on, each with its reason (see below) |

## Isolation

Daemons and coverage slots run in parallel. When tests share mutable state (a database), each slot needs its own
copy; otherwise a test of one slot sees the writes of another, and a mutant that broke a rollback poisons the next
run. `isolation` (an `init` argument) is an `IsolationSettings` value.

**`none`** (default): every slot shares the caller's environment.

**`postgres-template`**: one schema-only template database per migration fingerprint, cloned per slot.

```python
from balamut.config import DatabaseEnv, IsolationKind, IsolationSettings, init

init(
    isolation=IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE,
        migrate=("alembic", "upgrade", "head"),   # runs once per schema change, against the template
        migrate_env=(("APP_ENV", "test"),),       # extra environment of the migration command
        schema_inputs=("alembic/**", "alembic.ini"),  # tracked files whose content defines the schema
        locale="C",                               # LC_COLLATE / LC_CTYPE of the template
        env=DatabaseEnv(                          # names of the variables your settings read
            host="DB_HOST", port="DB_PORT", database="DB_NAME", user="DB_USER", password="DB_PASSWORD"
        ),
    ),
)
```

The configured database is only a source of connection parameters and is never cloned. The template is shared by
every run and every worktree with the same schema inputs; it is built under a temporary name and renamed atomically,
so concurrent runs see it whole or not at all. Templates unused for a week are evicted. Each slot gets its own
`DB_NAME`, and its clone is dropped when the slot ends. `psql` must be on `PATH`.

**`shell`**: delegate to your own commands.

```python
isolation=IsolationSettings(
    kind=IsolationKind.SHELL,
    prepare=("./scripts/make-slots",),  # prints a JSON array of BALAMUT_SLOTS objects mapping variables to values
    cleanup=("./scripts/drop-slots",),
)
```

Both commands run with `BALAMUT_SLOTS` (the count) and `BALAMUT_SLOT_PREFIX` (unique per campaign) set. `prepare`
must print exactly that many objects; object `k` is the environment overlay of slot `k`.

## Operators and kinds

A kind is a tuple of operator facts (frozen capability values) that enumerate it. The built-in kinds
(`src`, `concurrency`, `fault`, `oracle`, `extreme`, and the optional `db`/`chain`) live in balamut itself;
everything else is wired by the project's config file: `register` imports and registers (or overrides) a kind,
`remove` opts out.

```python
# balamut_conf.py
from balamut.config import QueueFamily, init
from balamut.operators.concurrency import (
    LockDrop, NowShiftCollapse, OnConflictDrop, QueueCompareDrop, SkipLockDrop,
)
from balamut.operators.fault import FaultRaiseAt, FaultReturnSwap

init(
    register={
        "concurrency": (
            LockDrop(),
            SkipLockDrop(),
            OnConflictDrop(),
            NowShiftCollapse(),
            QueueCompareDrop((
                QueueFamily(
                    model="Job",
                    family="jobs",
                    fence=("lease_token",),
                    due="run_at",
                    lease="lease_expires_at",
                    backstop="deadline",
                    progress="attempt",
                ),
            )),
        ),
        "fault": (
            FaultReturnSwap(return_swap=frozenset({"try_acquire"})),
            FaultRaiseAt(raise_at=frozenset({"commit", "flush", "rollback"})),
        ),
    },
    remove=frozenset({"oracle"}),  # never run the oracle kind here
)
```

`register` overrides a built-in kind or registers a custom one; a removed kind is unknown by design. Kind data
is typed data, never a mini-language: the queue families, the fault method names and the lock shapes are
constructed as values.

The survivor semantic classes (the `semantic` axis of the reports) are configured in the `semantic` argument of
`init`: a tuple of `(class, tokens)` pairs tagging a survivor by the text of the mutated code, plus the classes
a fact records at emission (the library namespace of the operator).

```python
init(semantic=(("money", ("amount", "price", "currency")),))
```

### Operator names

An operator name is `lib:area:op`, where `lib` names the library the construct comes from: `lang:` for core
syntax, `std:` for the standard library (`std:str:startswith->endswith`, `std:decimal:quantize-drop`,
`std:datetime:now-shift-collapse`, `std:asyncio:lock-drop`), `sa:` for SQLAlchemy (`sa:limit-drop`,
`sa:session:raise-at:commit`), `pytest:` for pytest (`pytest:raises`), and `project:` for project-domain
operators (`project:queue:jobs:fence-drop`). The mutant's family is a separate field; nothing parses operator
strings.

### Third-party kinds

A third-party kind ships as an ordinary importable package; a project wires it in its config file:

```python
# balamut_conf.py
from balamut.config import init
from balamut_precision import precision_kind

init(register={"precision": precision_kind})
```

The kind's name is the `register` key; its value is a tuple of facts (each a frozen value implementing the `Sites`
or `Enumerates` protocol of `balamut.operators.facts`); the facts' data is whatever their constructors take.
No base class, no registry beyond this dict, no config-table protocol: facts are ordinary frozen values, and
the config file is Python. `pip install`-ed packages need nothing more than to be importable.

## Accepted mutants

Some mutants cannot be killed: swapping the arguments of `max`, or deleting a `break` that only saves work,
changes no result. `accept` names them; `balamut diff` reports them apart and never fails on them.

```python
# balamut_conf.py
import re

from balamut.config import Accept, init

init(
    accept=(
        Accept(
            reason="max and min are commutative over numbers",
            operator="lang:call:arg-swap",
            original=re.compile(r"(max|min)\(.*\)"),
        ),
        Accept(
            reason="a chosen test gains nothing later: the delete only shortens the scan",
            path="src/pkg/suite.py",
            function="greedy_min_suite",
            operator="lang:statement:delete",
            original="del pool[best]",
        ),
    ),
)
```

| field | meaning |
|---|---|
| `reason` | why no test is owed; required |
| `path` | the file, relative to the project root |
| `function` | the qualified name of the enclosing function (`Class.method`, `outer.inner`) |
| `operator` | an operator name or a prefix of one |
| `original` | the mutated source text as a report prints it (runs of whitespace compare as one space), or a compiled pattern the text must match in full |

A rule needs a reason and at least one other field. An empty field matches anything, and the first rule that names
a mutant wins. A rule accepts a survivor, an uncovered site or an unresolved timeout; a killed mutant stays killed.
The gate prints the count (`accepted: N`) and lists every accepted mutant with its rule's reason under `accepted`
in the JSON report. `balamut mutate`, `balamut analyze` and the HTML report show the raw verdicts.

## Environment

Everything a run needs comes from the profile. The variables below are set by balamut for its own processes and
are listed only so they are not mistaken for configuration: `BALAMUT_MUTANT_FILE`, `BALAMUT_KILL_DEPS`,
`BALAMUT_TEST_ROOTS`, `BALAMUT_COVERAGE_*`, `PYTEST_FAST_*` and `PYTEST_ADDOPTS` (the mutation plugin is added to
it; a project marker filter is cleared, so a mutant is judged by every test that covers it).
