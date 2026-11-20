"""balamut's own dogfood profile: mutating balamut with its own suite.

Everything runs on the defaults; the measurement worker scrubs the coverage environment before
each test body, so the daemon and e2e tests (which spawn balamut themselves) run nested cleanly.

``accept`` lists the mutants no test is owed for, each with the reason. A rule that names an idiom
covers every site of it; a rule that names a path and a function covers one site.
"""

import re

from balamut.config import Accept, init

init(
    accept=(
        Accept(
            reason="max and min are commutative over the numbers they compare",
            operator="lang:call:arg-swap",
            original=re.compile(r"(max|min)\(.*\)"),
        ),
        Accept(
            reason="the order of the exception types a suppress names has no effect",
            operator="lang:call:arg-swap",
            original=re.compile(r"contextlib\.suppress\(.*\)"),
        ),
        Accept(
            reason="the wording of --help is documentation, not behaviour",
            operator="lang:call:kwarg-drop",
            original=re.compile(r"(, )?(help|description|prog)=.*"),
        ),
        Accept(
            reason="UTF-8 is already the text encoding of every locale the suite runs under",
            operator="lang:call:kwarg-drop",
            original=re.compile(r'(, )?encoding="utf-8",?'),
        ),
        # src/balamut/cli/analyze.py
        Accept(
            reason="--cache is only tested for truthiness: argparse's default None selects the default cache like ''",
            path="src/balamut/cli/analyze.py",
            function="main",
            operator="lang:call:kwarg-drop",
            original=', default=""',
        ),
        # src/balamut/cli/crosskill.py
        Accept(
            reason="--cache is only tested for truthiness: argparse's default None selects the default cache like ''",
            path="src/balamut/cli/crosskill.py",
            function="main",
            operator="lang:call:kwarg-drop",
            original=', default=""',
        ),
        Accept(
            reason=(
                "remaining holds only keys of pooled tests, so every pass over a non-empty remaining finds a positive"
                " gain and assigns best before it is read"
            ),
            path="src/balamut/cli/crosskill.py",
            function="greedy_min_suite",
            operator="lang:statement:delete",
            original="best: str | None = None",
        ),
        Accept(
            reason=(
                "an optimisation: the chosen test's kills have just left remaining, so its gain is zero and the strict"
                " gain > best_gain never picks it again"
            ),
            path="src/balamut/cli/crosskill.py",
            function="greedy_min_suite",
            operator="lang:statement:delete",
            original="del pool[best]",
        ),
        Accept(
            reason=(
                "unreachable defensive code: remaining holds only keys of pooled tests, so a pass over a non-empty"
                " remaining always assigns best"
            ),
            path="src/balamut/cli/crosskill.py",
            function="greedy_min_suite",
            operator="lang:control:break->continue",
            original="break",
        ),
        Accept(
            reason=(
                "the skip is an optimisation: a key never dominates itself (equal sets need inner < outer), and a"
                " dominated key's dominator dominates everything that key does, since domination is transitive"
            ),
            path="src/balamut/cli/crosskill.py",
            function="subsuming_mutants",
            operator="lang:boolop:Or->And",
            original="key_a == key_b or key_b in dominated",
        ),
        Accept(
            reason="inner == outer only for the key itself, which the preceding continue has already skipped",
            path="src/balamut/cli/crosskill.py",
            function="subsuming_mutants",
            operator="lang:compare:Lt->LtE",
            original="inner < outer",
        ),
        Accept(
            reason=(
                "an optimisation: dominated is a set, so scanning on after the first dominator adds the same key again"
            ),
            path="src/balamut/cli/crosskill.py",
            function="subsuming_mutants",
            operator="lang:control:break->continue",
            original="break",
        ),
        # src/balamut/cli/estimate.py
        Accept(
            reason=(
                "an early exit only: an empty row list falls through to an empty ranking, and both of its slices"
                " (ranked[:top], ranked[:keep + 2]) are empty too"
            ),
            path="src/balamut/cli/estimate.py",
            function="select_files",
            operator="lang:statement:delete",
            original="return []",
        ),
        Accept(
            reason="a keyword equal to the callee's default: argparse stores None for an absent option",
            path="src/balamut/cli/estimate.py",
            function="main",
            operator="lang:call:kwarg-drop",
            original=", default=None",
        ),
        Accept(
            reason=(
                "--daemons is only read as `args.daemons or resolve_workers()` (in main and in _probe_overhead), where"
                " argparse's default None selects the fallback like 0"
            ),
            path="src/balamut/cli/estimate.py",
            function="main",
            operator="lang:call:kwarg-drop",
            original=", default=0",
        ),
        # src/balamut/cli/mutate.py
        Accept(
            reason=(
                "--costs, --diff, --diff-scope, --refresh-failures-out, --invalidate-sites-from, --cache, --operator,"
                " --html and --stats-json are each tested for truthiness before their value is used, so argparse's"
                " default None reads like ''"
            ),
            path="src/balamut/cli/mutate.py",
            function="_parser",
            operator="lang:call:kwarg-drop",
            original=', default=""',
        ),
        Accept(
            reason="--limit is only read as `if args.limit:`, where argparse's default None reads like 0",
            path="src/balamut/cli/mutate.py",
            function="_parser",
            operator="lang:call:kwarg-drop",
            original=", default=0",
        ),
        Accept(
            reason=(
                "by_key holds only non-empty keys, so a record without a key misses it under '' and under None alike"
            ),
            path="src/balamut/cli/mutate.py",
            function="_cold_records.resolve",
            operator="lang:call:arg-drop",
            original=', ""',
        ),
        Accept(
            reason="tree_diverged is symmetric: it compares the two trees' hashes over the union of their paths",
            path="src/balamut/cli/mutate.py",
            function="_warn_stale_substrate",
            operator="lang:call:arg-swap",
            original="substrate.tree_diverged(recorded, substrate.tree_snapshot(layout))",
        ),
        Accept(
            reason=(
                "the cold count is only tested for truthiness when the breakdown has no cold entry: None skips the"
                " mechanisms line like 0"
            ),
            path="src/balamut/cli/mutate.py",
            function="_run",
            operator="lang:call:arg-drop",
            original=", 0",
        ),
        Accept(
            reason=(
                "cold_counts is read only by the agent-json and the default report, both of which imply needs_agent,"
                " where _cold_records has already reassigned it"
            ),
            path="src/balamut/cli/mutate.py",
            function="_emit",
            operator="lang:statement:delete",
            original="cold_counts: CountMap = {}",
        ),
        # src/balamut/cli/diff.py
        Accept(
            reason="metadata_mismatches compares the two records field by field with == and returns the field names",
            path="src/balamut/cli/diff.py",
            function="substrate_miss_reason",
            operator="lang:call:arg-swap",
            original="substrate.metadata_mismatches(previous, expected)",
        ),
        Accept(
            reason="metadata_mismatches compares the two records field by field with == and returns the field names",
            path="src/balamut/cli/diff.py",
            function="_doctor",
            operator="lang:call:arg-swap",
            original="substrate.metadata_mismatches(actual, expected_metadata(profile, isolation))",
        ),
        Accept(
            reason="the except handler falls through to the function's final return of the same 'invalid_metadata'",
            path="src/balamut/cli/diff.py",
            function="substrate_integrity_issue",
            operator="lang:statement:delete",
            original='return "invalid_metadata"',
        ),
        Accept(
            reason=(
                "a reuse already has no miss reason: every reason set before it (a forced rebuild, a confirmed"
                " baseline,"
                " a rebuilding drift, a seed) makes substrate_reused false, and substrate_ensure reuses a generation"
                " only when metadata_mismatches is empty"
            ),
            path="src/balamut/cli/diff.py",
            function="_Run._substrate",
            operator="lang:statement:delete",
            original="state.substrate_miss_reason = None",
        ),
        Accept(
            reason="--report-json is only tested for truthiness: argparse's default None selects the path like ''",
            path="src/balamut/cli/diff.py",
            function="_parser",
            operator="lang:call:kwarg-drop",
            original=', default=""',
        ),
        Accept(
            reason="the doctor report holds only ASCII text: the status word and the fixed issue codes",
            path="src/balamut/cli/diff.py",
            function="doctor_main",
            operator="lang:call:kwarg-drop",
            original=", ensure_ascii=False",
        ),
        Accept(
            reason="the doctor report holds only ASCII text: the status word and the fixed issue codes",
            path="src/balamut/cli/diff.py",
            function="doctor_main",
            operator="lang:const:bool:False->True",
            original="False",
        ),
        # engine/campaign.py
        Accept(
            reason=(
                "both breaks of the drain waves are early exits. A wave that finds the queue empty starts no thread,"
                " so the queue stays empty and every later wave skips the same way. A lane whose every mutant has a"
                " verdict has an empty queue: a worker requeues an item only when its lease raised, and a lease"
                " records its verdict as the last step that can raise (after a timeout sent to a retry only the"
                " stderr note follows), so the next wave leaves through the empty-queue check with the same results"
                " and the same wave count"
            ),
            path="src/balamut/engine/campaign.py",
            function="run_lane",
            operator="lang:control:break->continue",
            original="break",
        ),
        Accept(
            reason=(
                "unreachable: the check above proved that the result indices are exactly range(mutant_count), and the"
                " counter holds one entry per result, so the sum always equals mutant_count"
            ),
            path="src/balamut/engine/campaign.py",
            function="assert_campaign_complete",
            operator="lang:statement:delete",
            original='raise RuntimeError("the campaign does not hold exactly one terminal status per mutant")',
        ),
        Accept(
            reason=(
                "fresh_mode is read only under `if confirmation:`, and a confirmation is drained from `untouched`,"
                " which was then non-empty at the check that switches the pool and sets fresh_mode True: the initial"
                " value is never observed"
            ),
            path="src/balamut/engine/campaign.py",
            function="_execute",
            operator="lang:statement:delete",
            original="fresh_mode = False",
        ),
        Accept(
            reason=(
                "fresh_mode is read only under `if confirmation:`, and a confirmation is drained from `untouched`,"
                " which was then non-empty at the check that switches the pool and sets fresh_mode True: the initial"
                " False is never observed"
            ),
            path="src/balamut/engine/campaign.py",
            function="_execute",
            operator="lang:const:bool:False->True",
            original="False",
        ),
        Accept(
            reason=(
                "the switch under `if not fresh_mode:` is unreachable: a non-empty confirmation means `untouched` was"
                " non-empty at the first switch, which already ran and set fresh_mode True. The rule's text also names"
                " that first switch, whose deletion tests kill"
            ),
            path="src/balamut/engine/campaign.py",
            function="_execute",
            operator="lang:delete:call",
            original="switch_pool_to_fresh_workers([*normal_pool, *concurrency_pool])",
        ),
        Accept(
            reason=(
                "an early exit of the retry loop: with no timed-out mutant left nothing in the skipped body can add"
                " one, so every remaining retry skips the same way"
            ),
            path="src/balamut/engine/campaign.py",
            function="_execute",
            operator="lang:control:break->continue",
            original="break",
        ),
        Accept(
            reason=(
                "_execute sets PYTEST_ADDOPTS unconditionally before the try and nothing in between removes it: the"
                " pop in the finally always finds the key, so its default is never used"
            ),
            path="src/balamut/engine/campaign.py",
            function="_execute",
            operator="lang:call:arg-drop",
            original=", None",
        ),
        # engine/daemon.py
        Accept(
            reason=(
                "with timeout None the deadline is math.inf, which time.monotonic() never reaches: an unbounded wait"
                " never raises TimeoutExpired, so the 0.0 it would carry is never built"
            ),
            path="src/balamut/engine/daemon.py",
            function="_AdoptedProcess.wait",
            operator="lang:const:num:0.0->1.0",
            original="0.0",
        ),
        Accept(
            reason=(
                "json.dumps escapes non-ASCII by default and the payload is a hex token and a path, so the text is"
                " pure ASCII: every locale encoding writes the same bytes as UTF-8"
            ),
            path="src/balamut/engine/daemon.py",
            function="write_original_spec",
            operator="lang:call:arg-drop",
            original=', "utf-8"',
        ),
        Accept(
            reason=(
                "an empty subset returns before any request: process_failures is still the empty list created above,"
                " and the field's default is a fresh empty list too. The rule's text also names the same argument of"
                " the return after the request, whose drop tests kill"
            ),
            path="src/balamut/engine/daemon.py",
            function="run_subset",
            operator="lang:call:arg-drop",
            original=", process_failures",
        ),
        Accept(
            reason=(
                "ProcessLookupError and PermissionError are subclasses of OSError, which the same suppress still"
                " names: dropping either leaves the suppressed set unchanged. The rule's text also names"
                " ProcessLookupError in the suppress around proc.kill(), which has no OSError; tests kill that drop"
            ),
            path="src/balamut/engine/daemon.py",
            function="killpg",
            operator="lang:call:arg-drop",
            original=re.compile(r"(ProcessLookupError|PermissionError),"),
        ),
        Accept(
            reason=(
                "ProcessLookupError and PermissionError are subclasses of OSError, which the same suppress still"
                " names: dropping either leaves the suppressed set unchanged. The rule's text also names"
                " ProcessLookupError in the suppress around os.kill(), which has no OSError; tests kill that drop"
            ),
            path="src/balamut/engine/daemon.py",
            function="kill_stale_daemon",
            operator="lang:call:arg-drop",
            original=re.compile(r"(ProcessLookupError|PermissionError),"),
        ),
        # engine/planner.py
        Accept(
            reason=(
                "with pending == 0 every later branch clamps the daemon count to min(..., pending) == 0, so the"
                " fall-through builds the same ExecutionPlan(0, workers_per_daemon, 0)"
            ),
            path="src/balamut/engine/planner.py",
            function="resolve_execution_plan",
            operator="lang:compare:LtE->Lt",
            original="pending <= 0",
        ),
        # engine/timeouts.py
        Accept(
            reason="a cost of exactly zero contributes 0.0 to the maximum, which is the max's own default",
            path="src/balamut/engine/timeouts.py",
            function="mutant_timeout_budget",
            operator="lang:compare:GtE->Gt",
            original="seconds >= 0",
        ),
        # engine/stats.py
        Accept(
            reason=(
                "resolved is enumerated - executed + terminal, so resolved != enumerated holds exactly when"
                " terminal != executed: both operands always agree and `or` equals `and`"
            ),
            path="src/balamut/engine/stats.py",
            function="read_campaign_stats",
            operator="lang:boolop:Or->And",
            original="terminal != executed or resolved != enumerated",
        ),
        # engine/output.py
        Accept(
            reason=(
                "the looked-up group is only tested by `if not group: continue`: None and [] both skip the bucket."
                " The rule's text also names the same default inside len(...) of the summary line, whose drop tests"
                " kill"
            ),
            path="src/balamut/engine/output.py",
            function="flat_report",
            operator="lang:call:arg-drop",
            original=", []",
        ),
        # engine/identify.py
        Accept(
            reason=(
                "function_start_lineno and function_source resolve the function through the same _find_func lookup,"
                " so each of the two `return None` guards after them covers the other: without the first the second"
                " returns the same None, and the second is unreachable once the first passed (a parsed FunctionDef"
                " always has a source segment). The rule's text also names the module-level guard above them, whose"
                " deletion tests kill"
            ),
            path="src/balamut/engine/identify.py",
            function="identify",
            operator="lang:statement:delete",
            original="return None",
        ),
        # engine/worker.py
        Accept(
            reason="Timing.add only tests `if costs:`: an empty mapping and the default None both add nothing",
            path="src/balamut/engine/worker.py",
            function="_Lease.run",
            operator="lang:call:arg-drop",
            original=", {}",
        ),
        Accept(
            reason=(
                "original_process_failure is read only while the status is still KILLED, and every KILLED status"
                " went through _verify_kill, whose result reassigns it: the initial value is never observed"
            ),
            path="src/balamut/engine/worker.py",
            function="_Lease.run",
            operator="lang:statement:delete",
            original="original_process_failure = False",
        ),
        Accept(
            reason=(
                "the initial False of original_process_failure is never observed (read only while the status is"
                " KILLED, after _verify_kill reassigned it). The rule's text also names the initial False of"
                " rejected_kill one line above, whose flip tests kill"
            ),
            path="src/balamut/engine/worker.py",
            function="_Lease.run",
            operator="lang:const:bool:False->True",
            original="False",
        ),
        Accept(
            reason=(
                "pytest-fast answers rc 0 only when no test failed and no worker died, so with a green paired"
                " original `unstable` is empty, `confirmed` is the whole sub.killed_by and the fall-through returns"
                " the same tuple as the early return of a process kill. The rule's text also names the function's"
                " final return, whose deletion tests kill"
            ),
            path="src/balamut/engine/worker.py",
            function="_Lease._verify_kill",
            operator="lang:statement:delete",
            original="return MutantStatus.KILLED, VerdictReason.NONE, False, original_process_failure",
        ),
        # --- src/balamut/runtime/inmemory.py
        Accept(
            reason="ast.Name() without ctx builds the same Load() context (the node's default since Python 3.13)",
            path="src/balamut/runtime/inmemory.py",
            function="_YieldInjector.visit_Await",
            operator="lang:call:kwarg-drop",
            original=", ctx=ast.Load()",
        ),
        # --- src/balamut/runtime/bytecode.py
        Accept(
            reason=(
                "the statement above returns unless both instruction streams have the same length, so strict never"
                " fires"
            ),
            path="src/balamut/runtime/bytecode.py",
            function="_surgical_diff",
            operator="lang:call:kwarg-drop",
            original=", strict=True",
        ),
        Accept(
            reason=(
                "the statement above returns unless both instruction streams have the same length, so strict never"
                " fires"
            ),
            path="src/balamut/runtime/bytecode.py",
            function="_surgical_diff",
            operator="lang:const:bool",
            original="True",
        ),
        Accept(
            reason="every arm of the match that follows assigns result or returns before result is read",
            path="src/balamut/runtime/bytecode.py",
            function="mutate_code",
            operator="lang:statement:delete",
            original="result: types.CodeType | None = None",
        ),
        Accept(
            reason=(
                "the only open `return None` of mutate_code is the `case _` arm: BcEditKind has no fourth member, and"
                " falling through it leaves result None, which the next statement returns as None"
            ),
            path="src/balamut/runtime/bytecode.py",
            function="mutate_code",
            operator="lang:statement:delete",
            original="return None",
        ),
        # --- src/balamut/runtime/plugin.py
        Accept(
            reason=(
                "ast.literal_eval(None) raises the ValueError the next lines catch, so a spec without a literal is"
                " unsupported with or without the guard; the guard only keeps the call typed"
            ),
            path="src/balamut/runtime/plugin.py",
            function="_defaults_edit",
            operator="lang:condition:False",
            original="literal is None",
        ),
        Accept(
            reason=(
                "co_positions() reports a line and an end line together or neither (the compiler rejects a node with"
                " only"
                " one of them), so `or` and `and` skip the same instructions"
            ),
            path="src/balamut/runtime/plugin.py",
            function="_extent_holds",
            operator="lang:boolop:Or->And",
            original="start is None or end is None",
        ),
        Accept(
            reason=(
                "mutate_code only looks bc_source and bc_target up in its operator tables, where a missing name finds"
                ' nothing whether it reads as "" or as None'
            ),
            path="src/balamut/runtime/plugin.py",
            function="_bytecode_mutant",
            operator="lang:call:arg-drop",
            original=', ""',
        ),
        # --- src/balamut/runtime/kill_deps.py
        Accept(
            reason=(
                "lease_start assigns _lease before any read: record adds to it only once _leased is set, flush reads it"
                " only with a target, and lease_start sets all three"
            ),
            path="src/balamut/runtime/kill_deps.py",
            function="DepsRecorder.__init__",
            operator="lang:statement:delete",
            original="self._lease: set[KillDep] = set()",
        ),
        Accept(
            reason="flush reads _published only with a target, and lease_start assigns both together",
            path="src/balamut/runtime/kill_deps.py",
            function="DepsRecorder.__init__",
            operator="lang:statement:delete",
            original="self._published: set[KillDep] = set()",
        ),
        Accept(
            reason="include_attributes=False is the default of ast.dump",
            path="src/balamut/runtime/kill_deps.py",
            function="_digest",
            operator="lang:call:kwarg-drop",
            original=", include_attributes=False",
        ),
        Accept(
            reason=(
                "not equivalent, but no test can change the verdict: the mutated loop is the campaign worker's own"
                " recorder, which spins in its PY_START callback right after the code swap, before the touch marker is"
                " armed and before the first test of the lease starts, so the hang is never attributed (outside a"
                " campaign the frame-walking tests of test_kill_deps.py hang on it)"
            ),
            path="src/balamut/runtime/kill_deps.py",
            function="DepsRecorder._module_body_running",
            operator="lang:statement:delete",
            original="frame = frame.f_back",
        ),
        Accept(
            reason=(
                "starts and ends are built to the same length (ends is starts shifted by one plus the data length), so"
                " strict never raises"
            ),
            path="src/balamut/report/html.py",
            function="_Source.of",
            operator="lang:call:kwarg-drop",
            original=", strict=True",
        ),
        Accept(
            reason=(
                "starts and ends are built to the same length (ends is starts shifted by one plus the data length), so"
                " strict never raises"
            ),
            path="src/balamut/report/html.py",
            function="_Source.of",
            operator="lang:const:bool:True->False",
            original="True",
        ),
        Accept(
            reason=(
                "classes holds one entry per line of lines, so both sequences have the same length and strict never"
                " raises"
            ),
            path="src/balamut/report/html.py",
            function="highlight_lines",
            operator="lang:call:kwarg-drop",
            original=", strict=True",
        ),
        Accept(
            reason=(
                "classes holds one entry per line of lines, so both sequences have the same length and strict never"
                " raises"
            ),
            path="src/balamut/report/html.py",
            function="highlight_lines",
            operator="lang:const:bool:True->False",
            original="True",
        ),
        Accept(
            reason=(
                "values, and the scored pairs they come from, are built to the same length by one comprehension, so"
                " strict never raises"
            ),
            path="src/balamut/report/html.py",
            function="_trend",
            operator="lang:call:kwarg-drop",
            original=", strict=True",
        ),
        Accept(
            reason=(
                "values, and the scored pairs they come from, are built to the same length by one comprehension, so"
                " strict never raises"
            ),
            path="src/balamut/report/html.py",
            function="_trend",
            operator="lang:const:bool:True->False",
            original="True",
        ),
        Accept(
            reason=(
                "a token type outside _TOKEN_CLASS yields None instead of an empty class and is skipped by the same"
                " truthiness test; an exhausted line feed raises StopIteration in the readline callback, which"
                " tokenize reads as end of input exactly like the empty string"
            ),
            path="src/balamut/report/html.py",
            function="highlight_lines",
            operator="lang:call:arg-drop",
            original=', ""',
        ),
        Accept(
            reason=(
                "an empty class marks nothing, and _mark's row range is empty for a token that starts past the last"
                " line, so either operand alone changes no output"
            ),
            path="src/balamut/report/html.py",
            function="highlight_lines",
            operator="lang:boolop:And->Or",
            original="css and token.start[0] <= len(lines)",
        ),
        Accept(
            reason="a diff of zero returns the flat marker before these tests, so diff is never 0 here and > equals >=",
            path="src/balamut/report/html.py",
            function="_delta",
            operator="lang:compare:Gt->GtE",
            original="diff > 0",
        ),
        Accept(
            reason="check=False is the default of subprocess.run",
            path="src/balamut/gitutil.py",
            function="git_output_in",
            operator="lang:call:kwarg-drop",
            original=", check=False",
        ),
        Accept(
            reason='"big" is the default byteorder of int.from_bytes on every supported Python (3.14+)',
            path="src/balamut/concurrency.py",
            function="_hashed",
            operator="lang:call:arg-drop",
            original=', "big"',
        ),
        Accept(
            reason=(
                "ast.get_source_segment returns None for a node without position attributes, so a function that is"
                " not found reads as None with or without the early return"
            ),
            path="src/balamut/core/hashing.py",
            function="function_source",
            operator="lang:statement:delete",
            original="return None",
        ),
        Accept(
            reason=(
                "no def or class is ever named <locals>: without the early return the scan below matches nothing"
                " and returns None as well (the other return None of the function is killed by a test)"
            ),
            path="src/balamut/core/hashing.py",
            function="_find_func",
            operator="lang:statement:delete",
            original="return None",
        ),
        Accept(
            reason=(
                "the trailing-backslash branch runs only on the last character of the path: after index += 1 the"
                " loop condition is false, so continue and break both leave the loop (the three other continue"
                " statements of the function are killed by tests)"
            ),
            path="src/balamut/diff/scope.py",
            function="_git_unquote",
            operator="lang:control:continue->break",
            original="continue",
        ),
        Accept(
            reason=(
                "a None parent matches no assignment pattern: the match statement falls to its default case, which"
                " returns False as well (that default return itself is killed by a test)"
            ),
            path="src/balamut/operators/kit.py",
            function="AssignmentTarget.matches",
            operator="lang:statement:delete",
            original="return False",
        ),
        Accept(
            reason=(
                "the loop only skips whitespace before the keyword: with > it stops on a whitespace byte at offset 0"
                " instead of running to -1, and the return below yields None for both (offset 0 holds no comma); the"
                " same comparison in that return is killed by a test"
            ),
            path="src/balamut/operators/kit.py",
            function="_separator_before",
            operator="lang:compare:GtE->Gt",
            original="position >= 0",
        ),
        Accept(
            reason=(
                "the span passed is node_byte_span(target, ctx.line_starts), exactly what emit computes itself when"
                " no span is given"
            ),
            path="src/balamut/operators/kit.py",
            function="RegionRewrite.enumerate",
            operator="lang:call:kwarg-drop",
            original=", span=span",
        ),
        Accept(
            reason=(
                "a parsed integer literal is never negative (a minus sign parses as a UnaryOp), so == 0 and <= 0 hold"
                " for the same literals"
            ),
            path="src/balamut/operators/kit.py",
            function="_is_zero",
            operator="lang:compare:Eq->LtE",
            original="node.value == 0",
        ),
        Accept(
            reason="a display shorter than two elements returned above, so <= 2 holds exactly when == 2 does",
            path="src/balamut/operators/kit.py",
            function="DropLastElementSite.find",
            operator="lang:compare:Eq->LtE",
            original="len(items) == 2",
        ),
        Accept(
            reason=(
                "the parse only tells whether the cut left a bracket unmatched, and the comma that keeps a"
                " two-element tuple a tuple changes no bracket"
            ),
            path="src/balamut/operators/kit.py",
            function="DropLastElementSite.find",
            operator="lang:binop:Add->lhs",
            original="data[whole[0] : previous[1]] + text.encode()",
        ),
        Accept(
            reason=(
                "the parsed text is a slice of a module that parsed, cut between tokens, so ast.parse rejects it only"
                " with a SyntaxError"
            ),
            path="src/balamut/operators/kit.py",
            function="DropLastElementSite.find",
            operator="lang:except:Exception",
            original="SyntaxError",
        ),
        # --- src/balamut/operators/src.py
        Accept(
            reason="the number of words that differ is the same counted from either name",
            path="src/balamut/operators/src.py",
            function="_alike",
            operator="lang:call:arg-swap",
            original="zip(words, others, strict=True)",
        ),
        Accept(
            reason="the condition before checks that both names have as many words, so strict never fires",
            path="src/balamut/operators/src.py",
            function="_alike",
            operator="lang:call:kwarg-drop",
            original=", strict=True",
        ),
        Accept(
            reason="the condition before checks that both names have as many words, so strict never fires",
            path="src/balamut/operators/src.py",
            function="_alike",
            operator="lang:const:bool",
            original="True",
        ),
        Accept(
            reason="_alike is symmetric: two names are alike in either order",
            path="src/balamut/operators/src.py",
            function="_closest",
            operator="lang:call:arg-swap",
            original="_alike(name, other)",
        ),
        Accept(
            reason=(
                "a None parent has no suites: _suite_candidates returns [] for it and suite_of falls through to its"
                " final return None"
            ),
            path="src/balamut/operators/facts.py",
            function="EnumContext.suite_of",
            operator="lang:statement:delete",
            original="return None",
        ),
        Accept(
            reason=(
                "only a line without a line event reaches the holders, and the first line of a code object's extent"
                " starts one of its instructions, so it has an event: the line never equals code.first there"
            ),
            path="src/balamut/coverage/lines.py",
            function="LineEvents.recorded_as",
            operator="lang:compare:LtE->Lt",
            original="code.first <= line <= code.last",
        ),
        Accept(
            reason=(
                "only a line without a line event is matched against the extents, and an extent's first line is the"
                " line number of its instruction, so it has an event: the line never equals first there"
            ),
            path="src/balamut/coverage/lines.py",
            function="LineEvents.recorded_as",
            operator="lang:compare:LtE->Lt",
            original="first <= line <= last",
        ),
        Accept(
            reason=(
                "co_positions() reports a line and an end line together or neither (the compiler rejects a node with"
                " only one of them), so `or` and `and` keep the same extents"
            ),
            path="src/balamut/coverage/lines.py",
            function="line_events",
            operator="lang:boolop:And->Or",
            original="first is not None and last is not None",
        ),
        Accept(
            reason=(
                "without the early return the except branch falls out of the try statement into the final return of the"
                " same value"
            ),
            path="src/balamut/coverage/coverage_map.py",
            function="_relativize",
            operator="lang:statement:delete",
            original="return measured",
        ),
        Accept(
            reason=(
                "cache hit only: without it the function recomputes the same answer from the file, which does not"
                " change within a run"
            ),
            path="src/balamut/coverage/discovery.py",
            function="_defines_collectable_tests",
            operator="lang:statement:delete",
            original="return known",
        ),
        Accept(
            reason=(
                "the cache write only avoids rereading a file: the answer for a file is the same every time it is"
                " computed"
            ),
            path="src/balamut/coverage/discovery.py",
            function="_defines_collectable_tests",
            operator="lang:statement:delete",
            original="cache[def_file] = defines",
        ),
        Accept(
            reason=(
                "without an any-import occurrence module_import (a narrower pattern over the same text) finds nothing"
                " either, so the count comparison and the loop below return False as well"
            ),
            path="src/balamut/coverage/discovery.py",
            function="_wrapper_may_reexport",
            operator="lang:statement:delete",
            original="return False",
        ),
        Accept(
            reason=(
                "the bytes matched by the ASCII-only \\w pattern are always valid UTF-8, so the error handler is"
                " never used"
            ),
            path="src/balamut/coverage/discovery.py",
            function="_wrapper_may_reexport",
            operator="lang:call:arg-drop",
            original=', "replace"',
        ),
        Accept(
            reason=(
                "with no module roots no import can start with '<root>.', so the loop below indexes nothing and builds"
                " the same empty snapshot; the early return only skips the file scan"
            ),
            path="src/balamut/coverage/discovery.py",
            function="_snapshot_build",
            operator="lang:statement:delete",
            original="return ReexportSnapshot(index=MappingProxyType(index), by_file=MappingProxyType({}))",
        ),
        Accept(
            reason=("None is the default of dict.get on the module namespace"),
            path="src/balamut/coverage/fast_plugin.py",
            function="source_cache_clears",
            operator="lang:call:arg-drop",
            original=", None",
        ),
        Accept(
            reason=(
                "a falsy None and False both exclude a module without a str __file__; the only reader is a boolean"
                " filter"
            ),
            path="src/balamut/coverage/fast_plugin.py",
            function="source_cache_clears.measured",
            operator="lang:return:None",
            original="return False",
        ),
        Accept(
            reason=("CoverageData.update on a not yet read shard opens and reads the database on first use"),
            path="src/balamut/coverage/fast_plugin.py",
            function="combine_worker_artifacts",
            operator="lang:delete:call",
            original="shard.read()",
        ),
        Accept(
            reason=("CoverageData.write is a no-op for the SQLite data file: update already stored every row"),
            path="src/balamut/coverage/fast_plugin.py",
            function="combine_worker_artifacts",
            operator="lang:delete:call",
            original="combined.write()",
        ),
        Accept(
            reason=("original_environment always sets PYTEST_ADDOPTS, so the default of the lookup is never used"),
            path="src/balamut/coverage/measure.py",
            function="_run_slots",
            operator="lang:call:arg-drop",
            original=', ""',
        ),
        Accept(
            reason=(
                "metadata_compatible only tests that no field differs, and every comparison in metadata_mismatches is"
                " an equality: symmetric in its arguments"
            ),
            path="src/balamut/coverage/pool.py",
            function="_candidate",
            operator="lang:call:arg-swap",
            original="substrate.metadata_compatible(metadata, expected)",
        ),
        Accept(
            reason=(
                "every comparison in metadata_mismatches is an equality: the list of differing names is the same in"
                " either argument order"
            ),
            path="src/balamut/coverage/substrate.py",
            function="metadata_compatible",
            operator="lang:call:arg-swap",
            original="metadata_mismatches(actual, expected)",
        ),
        Accept(
            reason=("metadata_compatible reduces symmetric equalities to a boolean: the same in either argument order"),
            path="src/balamut/coverage/substrate.py",
            function="substrate_ensure",
            operator="lang:call:arg-swap",
            original="metadata_compatible(current, expected)",
        ),
        Accept(
            reason=(
                "tree_diverged is the set of paths whose hashes differ between the two trees: symmetric in its"
                " arguments"
            ),
            path="src/balamut/coverage/substrate.py",
            function="drift_between",
            operator="lang:call:arg-swap",
            original="tree_diverged(recorded, current)",
        ),
        Accept(
            reason=("json.dumps escapes non-ASCII by default (ensure_ascii=True)"),
            path="src/balamut/coverage/refresh.py",
            function="_targets_summary",
            operator="lang:call:kwarg-drop",
            original=", ensure_ascii=True",
        ),
        Accept(
            reason=("json.dumps escapes non-ASCII by default (ensure_ascii=True)"),
            path="src/balamut/coverage/refresh.py",
            function="_targets_note",
            operator="lang:call:kwarg-drop",
            original=", ensure_ascii=True",
        ),
        Accept(
            reason=(
                "measure_coverage, called next with a file in the scratch directory, creates that directory with"
                " parents as its first step"
            ),
            path="src/balamut/coverage/refresh.py",
            function="measure_test_coverage",
            operator="lang:delete:call",
            original="scratch.mkdir(parents=True, exist_ok=True)",
        ),
        Accept(
            reason=(
                "executescript commits the open transaction itself and the schema statements autocommit, so the"
                " explicit commit publishes nothing more"
            ),
            path="src/balamut/coverage/store.py",
            function="Store.__init__",
            operator="lang:delete:call",
            original="self._db.commit()",
        ),
        Accept(
            reason=("the executescript that follows commits the open transaction before it runs"),
            path="src/balamut/coverage/store.py",
            function="Store._migrate_if_stale",
            operator="lang:delete:call",
            original="self._db.commit()",
        ),
        Accept(
            reason=("executemany over no keys changes nothing, so the count difference below is 0 as well"),
            path="src/balamut/coverage/store.py",
            function="Store.delete_mutants",
            operator="lang:statement:delete",
            original="return 0",
        ),
        Accept(
            reason=(
                "without a generation the query below compares generation to NULL, which matches no row, and returns {}"
                " as well"
            ),
            path="src/balamut/coverage/store.py",
            function="Store.get_file_hashes",
            operator="lang:statement:delete",
            original="return {}",
        ),
        Accept(
            reason=("with no rows both id lookups return {} and both executemany calls insert nothing"),
            path="src/balamut/coverage/store.py",
            function="Store._coverage_rows_insert",
            operator="lang:statement:delete",
            original="return",
        ),
        Accept(
            reason=(
                "the Import branch yields its names and the check below returns for every node that is not an"
                " ImportFrom"
            ),
            path="src/balamut/coverage/support_deps.py",
            function="imported_modules",
            operator="lang:statement:delete",
            original="return",
        ),
        Accept(
            reason=("the filename only labels a SyntaxError of ast.parse, which file_references turns into None"),
            path="src/balamut/coverage/support_deps.py",
            function="file_references",
            operator="lang:call:kwarg-drop",
            original=", filename=relative",
        ),
    ),
)
