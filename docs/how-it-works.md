# How it works

## The coverage substrate

Mutation testing needs to know which tests reach each line. balamut measures **per-test line coverage** of the
whole suite once: one pytest-fast run per isolation slot, each forked worker recording exact per-test contexts with
coverage.py's C tracer, the shards merged fail-closed (a missing or extra worker shard aborts the merge). With the
coverage it records each test's full duration (setup, call and teardown) and the hashes of every source and test
file it measured: the **tree record**.

The tracer records a line only when CPython emits a line event for it. A line without one, such as a constant
argument CPython folds into one constant with its neighbours, is covered by the tests recorded on the line of the
innermost instruction that spans it.

The triple (coverage, costs, metadata) plus the tree record is published atomically as a new *generation* under a
file lock. A substrate is reused while it was measured the same way (substrate schema, configuration files and
layout, Python and the measuring packages, the isolation's schema fingerprint). The set of test files and their
bodies are deliberately not part of that check: they are what a refresh handles.

Every worktree of a repository shares a small **pool** of recent substrates in the git common directory. A tree
without a compatible substrate of its own is seeded from the closest pooled one instead of a full build, and the
divergence is refreshed.

## Refresh

Before a campaign the coverage store is brought up to date with the current tree, re-measuring as little as
possible:

* an edited source file re-measures the tests that covered it in any version this generation saw;
* an edited test file is re-measured whole (new, changed and removed tests);
* edited test support (a helper or conftest module) re-measures the test files that import it, transitively;
* files outside the diff that diverged from the tree record (after a rebase, say) are refreshed the same way;
* a source file the build never saw, or a divergence above a quarter of the tree, rebuilds the substrate instead.

A direct campaign (`balamut mutate PATHS`) refreshes the same way with an empty diff: every file diverged from the
tree record is refreshed, so a test written after the last campaign already covers this one. A missing substrate is
built; `--no-refresh-substrate` reads the substrate as is and only warns about the divergence.

Content hashes make a repeated refresh free. A module imported before the tracer started (through conftest and the
application bootstrap) can be under-measured by an isolated re-run; a file that a refresh would leave with no
coverage at all gets its baseline rows back.

## The engine

A campaign enumerates the mutants of its paths and splits them into cached verdicts and work:

* the **cache key** addresses the module, the mutated function's text, the operator, the mutated text and the
  site's offset in the function, so moving a function within its file keeps its verdicts while identical
  functions of two modules keep one verdict each;
* a cached verdict holds while its **covering hash** matches: the test-support corpus, the mutant's file, the
  source files its covering tests execute, the covering test files and the covering node ids;
* a kill with **recorded dependencies** holds instead while the functions the killing run actually entered are
  unchanged (their AST, the module skeletons and the support corpus), so editing a neighbouring function of the
  same module does not re-run it.

The work runs on a pool of resident pytest-fast daemons. Each collects only the covering test files once, with the
mutation plugin loaded, and keeps a warm worker pool across requests. Per mutant the driver writes a spec into the
daemon's mutant file and requests the covering tests, previous killers first and then by measured cost, with
stop-first: the daemon stops handing out tests after the first real failure.

In the worker the plugin patches the target function's `__code__` (or `__defaults__`): a size-preserving bytecode
edit where possible, a recompiled function otherwise. A nested function is patched through its top-level ancestor.
The edit is live only while a test runs: applied at its setup, reverted after its teardown, so the plugin code that
applies, reverts and observes a mutant never runs it, even when balamut mutates itself. At every mutant boundary the
safe Python caches are cleared and the bindings an `importlib.reload` made during the previous mutant are put back.
A reload of the mutated module during a mutant's tests carries the edit to the definitions it binds.

## Verdict rules

* **Paired original.** A candidate kill re-runs the killers on the immediately restored original in the same
  worker state. Only killers that pass there count; a test that fails on the original too proves nothing. Such a
  worker carries state an earlier request left behind, so the next mutant runs in a new one.
* **Touch marker.** `sys.monitoring` marks the first entry into the mutated code object. A warm verdict whose run
  never entered it (a session fixture may have computed a value on the original in an earlier request) is
  confirmed in a fresh fork with fresh isolation slots. If even the fresh run never enters it, the mutant is
  `unsupported`, never a survivor. The marker also watches the original code object at the site: a closure or a
  copy of the function made before the mutant (a decorator's wrapper built when a test module was imported) keeps
  the original body, and a survivor whose tests entered it is `unsupported` at once, as a fresh fork inherits the
  same objects.
* **Process-level kills.** A worker that dies mid-test is a kill only when pytest-fast proves the active test item
  and it follows the complete prefix of reported results; the original must then pass in a new process.
* **Hangs.** A request that stops reporting for longer than its budget is a hang. The first hang goes to an
  isolated retry with an escalated budget; a final hang is a kill only when the mutant ran, the hung test is known,
  and that test passes on the original in the same budget. Otherwise it stays `timeout` with a reason
  (`hang_unconfirmed`, `original_hang`, `original_failed`).
* **Exactness.** Every campaign checks it has exactly one terminal verdict per mutant. A protocol answer it cannot
  interpret (rc 1 with no failing report and no active-item evidence, a malformed result) aborts the campaign.

Concurrency mutants run with one worker per daemon, in parallel isolated lanes when the project has isolation slots.

## The gate

`balamut diff` pins the scope first: the changed lines of every changed Python file against the merge base, and
the hash of each file's content. It then runs the preflight, checks or builds the substrate, confirms a cached
baseline failure with a focused run of its test files (a red baseline never needs a full rebuild to be reported
again), and runs `balamut mutate` over the scope. The engine checks the scope at every mutant boundary and stops at
the first edit; the gate checks it again at the end. Moving refs alone (a `git fetch`, a new commit of the same
content) never invalidates the verdict.

Only survivors in decision, arithmetic, state and return sites (the `high` bucket), uncovered sites inside function
bodies and unresolved timeouts gate. Module- and class-level sites without coverage run at import time and are
reported separately as cold, without gating. A mutant the project's `accept` rules name (an equivalent mutant, with
the reason in the config file) is reported apart and does not gate.
