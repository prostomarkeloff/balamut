# PyMutBench harness

The code and data behind [docs/benchmark.md](../docs/benchmark.md). The harness itself is stdlib-only
Python 3.14; everything that executes third-party code (subject projects, their test suites and the mutation
tools) runs in Docker images built from a digest-pinned `python:3.14` base, with the network disabled except
for image builds.

## Layout

| path | what it is |
|---|---|
| `Dockerfile` | the base image (CPython 3.14, git, uv) |
| `pymutbench/tools.toml`, `registry.py` | the benchmarked tools: release, install requirements, environment, adapter, default configuration |
| `env/` | the measured versions of every environment and the commit of every subject tree (`orchestrate snapshot-env`); image builds install with them as constraints |
| `pymutbench/subjects.py` | the subject projects: repository, packages, test environment, test roots |
| `pymutbench/validate.py` | mining and validation of real-fault candidates (runs in a subject image) |
| `pymutbench/review.py` | the adjudication rubric, the raters' agreement, the human label audit and the survivor noise sample |
| `pymutbench/scope.py` | the mutation scope of a bug: the definitions its fix touched |
| `pymutbench/fixes.py`, `fixlines.py` | a bug's fix record (sources, `-U0` diffs, commit dates, AI-tool markers); inserted and rewritten lines, witness classes, transplants into the buggy file, scope noise |
| `pymutbench/adapters/` | one generation adapter per tool: the tool's own generator, every native mutant as a mutated file |
| `pymutbench/astmat.py` | verified source materialization of AST-level mutants |
| `pymutbench/oracle.py` | the reference oracle (bug mode on the fixed or the buggy side, suite mode) |
| `pymutbench/prefix.py`, `generate_buggy.py` | the third condition: the buggy-side counterparts of the witnesses |
| `pymutbench/plugin_path/pmb_pytest_plugin.py` | the pytest plugin of every reference run |
| `pymutbench/native.py` | native campaigns: each tool's own engine, timed, its verdicts mapped to artifacts |
| `pymutbench/metrics.py`, `stats.py`, `report.py`, `report_engine.py`, `svg.py` | measures, statistics, tables and figures |
| `pymutbench/restore.py`, `check_rebuild.py` | the working outputs rebuilt from a release's data; the rebuilt report compared with the published one |
| `pymutbench/submit.py`, `mutant.schema.json`, `SUBMISSION.md` | a tool outside the harness evaluated from its own mutants |
| `conformance/` | small projects with one execution trap each, and their expected verdicts |
| `tests/` | unit tests of the harness (`python -m unittest discover -s bench/tests`) |
| `results/` | the published data of the current release |

## Versions

A result names three versions (`pymutbench/__init__.py`): the corpus (`CORPUS`, the frozen bugs, tests and
labels; a bug id `<project>@<commit12>` never changes, and a withdrawn bug is marked, not renumbered), the
harness (`__version__`) and the results release (`RESULTS`). [`CITATION.cff`](CITATION.cff) cites them;
[`SUBMISSION.md`](SUBMISSION.md) gives the fields a quoted result must carry; [`CONTRIBUTING.md`](CONTRIBUTING.md)
the corrections and the corpus update policy.

## The numbers from the published data

```console
$ export PMB_WORK=~/pymutbench        # working outputs (outside the repository)
$ cd bench
$ python3 -m pymutbench.restore --results results          # $PMB_WORK/out from results/data
$ python3 -m pymutbench.report bugs --results /tmp/results --images /tmp/images
$ python3 -m pymutbench.check_rebuild results /tmp/results ../docs/images /tmp/images
```

`restore` writes every input of `report bugs` (the selection, the labels, per bug its fix record, scope,
artifacts, ledgers, verdicts and buggy-side counterparts); the engine track's summary beside `S(k)` comes from the
published `results/engine_track.json` (`--engine-track`). The report needs no clone and no container; the CI job
checks that the rebuilt report equals the published one byte for byte.

## Reproducing a release

```console
$ export PMB_WORK=~/pymutbench
$ cd bench
$ python3 -m pymutbench.orchestrate build-base
$ python3 -m pymutbench.orchestrate build-subject attrs click ...     # at the recorded commit, with env/ versions
$ uv build --wheel -o .wheels .. && python3 -m pymutbench.orchestrate build-tools
$ python3 -m pymutbench.orchestrate validate-all --list                # every subject's candidates, mainline order
$ python3 -m pymutbench.orchestrate validate-all --containers 4 --jobs 3                  # screening
$ python3 -m pymutbench.review build --per-subject 25 --batch-size 5   # raters B and C, see below
$ python3 -m pymutbench.review build-tiebreak --batch-size 5
$ python3 -m pymutbench.review rate-codex --effort medium --inline --inputs inputs_tiebreak --rater tiebreak
$ python3 -m pymutbench.review finalize                                # final.jsonl, agreement.json
$ python3 -m pymutbench.orchestrate validate-all --stage validate3 --recheck validate     # final protocol
$ python3 -m pymutbench.pipeline select                                # bugs/<subject>.jsonl, bugs/selection.json
$ python3 -m pymutbench.review build-purity                            # purity packets of the selected bugs
$ python3 -m pymutbench.pipeline generate --parallel 3
$ export PMB_MEMORY=2500m                # every judging container's memory limit (unset: no limit)
$ python3 -m pymutbench.pipeline judge --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline confirm --parallel 3 --jobs 4   # every witness re-judged from fresh trees
$ python3 -m pymutbench.pipeline sensitivity --sample 1000   # stage 0/1 kills re-judged by the full suite alone
$ python3 -m pymutbench.pipeline equivalence --sample 1000   # judged artifacts re-judged by the current oracle code
$ python3 -m pymutbench.pipeline prefix --parallel 2 --jobs 2  # untouched-code witnesses on the buggy parent
$ python3 -m pymutbench.pipeline buggy --parallel 1            # the generators on the buggy files (rewritten lines)
$ python3 -m pymutbench.pipeline prefix --parallel 2 --jobs 2  # their counterparts judged
$ python3 -m pymutbench.report bugs --results results --images ../docs/images
```

Raters B and C are Claude agents (Opus and Sonnet) given `review/rubric.md`, `review/schema.json` and the batch
files, each blind to the other rater and to every tool result; they write `review/claude/` and
`review/claude_c/` in that schema. The purity labels come the same way: one Claude agent per batch of
`review/purity_inputs/`, given `review/purity_rubric.md` and `review/purity_schema.json`, writes
`review/purity/`; a selected bug without a purity label blocks the report. `review human-sample` draws the human
label audit (every bug the third rater decided, the rest per project) with blind packets for two people, and
`review human-agreement` compares their labels with each other and with the models'; `review survivor-sample`
draws, per tool, surviving non-witness mutants for a manual label (equivalent, test gap, uninteresting).

`select` reports, per subject, every candidate ahead of the last selected bug (every candidate, while the cap is
not reached) that is not yet validated, adjudicated or re-validated: `validate-all --only`, `review build
--subjects` and `validate-all --recheck validate --only` process exactly those. Every stage is resumable: a bug or
a campaign whose output exists is skipped, and a partly judged bug continues from its verdict file. Run `judge`
again until every bug is complete; a bug whose container hit the memory limit stays incomplete, so resume it alone
with `--jobs 1`. A generation adapter killed by the memory limit is recorded as failed: generate that tool again
without `PMB_MEMORY` (inflect's module needs more than 2.5 GB under irradiate).

Each driver process copies `pymutbench/` once into `$PMB_WORK/snapshots/<digest>` and mounts that copy as
`/bench`, so editing the working tree never changes a running stage. Generation ledgers carry a fingerprint of the
code behind each tool's mutants (the shared generation files and its adapter), with the image identity; a changed
adapter means `generate --tools <tool> --force`, and `report` refuses a tool whose ledgers carry two
fingerprints. Oracle records carry the version of their mode's verdict semantics (`fingerprint.ORACLE_SEMANTICS`:
`bug/1`, `parent/1`, `suite/1`) and, as a reference, the fingerprint of the oracle's code; a judging run appends to
a verdict file and `report` aggregates verdicts only of one semantics, so a change of the oracle code that cannot
change a mode's verdicts judges new artifacts without judging the corpus again, and one that can raises the
version. Verdicts written before the field existed are read through `fingerprint.LEGACY_ORACLE`, which
`pipeline equivalence` checks by judging a seeded sample again. `report` publishes only a complete corpus: a final
selection (or subjects named with `--accept-incomplete`), a purity label for every selected bug, every tool
artifact judged, every witness re-judged, every buggy-side counterpart built and judged, both oracle samples
judged, one fingerprint per tool and one semantics per oracle mode; `--preview` lists the gaps instead.

The third condition runs in two passes. `prefix` classifies every witness by where it lies relative to the fix,
writes each one in untouched code into the buggy parent's file (`parent.union.jsonl`, `prefix.json`) and judges
them with the historical suite on the parent tree (`oracle.parent.jsonl`); witnesses on rewritten lines stay
pending until `buggy` runs each tool's generator on the buggy files (`<bug>/buggy/`), and the second `prefix`
matches and judges their counterparts.

The engine and conformance tracks time each tool's own campaign, one container at a time, in images built per
project (with the test dependencies a project's suite needs at HEAD, `orchestrate.NATIVE_TEST_EXTRAS`) and for
the trap fixtures. `PMB_ENGINE_CPUS=0-3` puts each campaign container on those cores and `PMB_ENGINE_MEMORY=3g`
caps it, so a campaign that exhausts memory fails inside it; a project's reference oracle runs after its
campaigns, on the test roots they run and on `PMB_CPUS`, and judges the union of their mutants in one run. A
repetition (`--reps N`) runs the campaigns again in an order shuffled per repetition; the report gives the median
and the range of each campaign's wall time and the verdicts that changed between runs, and records the CPUs a
campaign could use against those its tools saw and the most tasks its container held. A campaign that leaves no
report is recorded as failed (`<tool>.r1.failed.json`), and a rerun skips it as it skips a report: to run a
campaign again, delete its report or failure record. The reference run has no resume, so a union with an
artifact it has not judged is judged again whole. A reference run that does not finish prints its exit status;
137 means the out-of-memory killer took the oracle process itself, and the `engine` command below with the
project's name and `--jobs 1` judges that union again one mutant at a time. `report_engine` refuses a planned
campaign or oracle run that is missing and an artifact without a reference verdict; `--preview` lists the gaps
instead; `--focus TOOL` highlights one tool in the figures:

```console
$ python3 -m pymutbench.orchestrate build-native python-semver cachetools humanize toolz sqlparse attrs
$ python3 -m pymutbench.orchestrate build-conformance
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline engine --reps 1
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline engine cachetools --reps 5
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline conformance
$ python3 -m pymutbench.report_engine engine --results results --images ../docs/images
$ python3 -m pymutbench.report_engine conformance --results results --images ../docs/images
```

A new trap fixture joins the measured trap image with `orchestrate add-fixtures --measured <tag>`: the image is
kept under the tag and extended with the new fixture, so the other fixtures' environments and verdicts stay as
measured.

## Adding a tool

A tool outside the harness is evaluated from its own mutants, without changing the harness
([`SUBMISSION.md`](SUBMISSION.md)). To benchmark it in the harness:

1. Add its entry to `pymutbench/tools.toml` (release, install requirements, environment name, adapter, the engine
   in one line).
2. Write `pymutbench/adapters/ad_<tool>.py`: call the tool's own generator on `--files` under `--root` and emit
   one JSON line per native mutant with the complete mutated text (`_common.emit`, the format of
   `mutant.schema.json`). A mutant that has no faithful plain-source form is emitted with a `status` explaining
   why, never dropped.
3. Add the campaign to `pymutbench/native.py`: the tool's documented quick start, its report parsed into the
   normalized verdicts (killed, survived, timeout, no_coverage, error, skipped).
4. Rebuild the images and run the pipeline; ask the tool's maintainers to confirm the configuration.

A new tool changes the union of judged mutants, so it is published as a new result release; earlier releases
stay as they were.

## A new release of a benchmarked tool

A tool's new release is measured in the images of the release it replaces, so only the tool's own environment
changes. For balamut: move the old wheel out of `.wheels`, name the new one in `pymutbench/tools.toml`, then

```console
$ uv build --wheel -o .wheels ..
$ python3 -m pymutbench.orchestrate refresh-tool balamut --measured balamut-0.1.5 --unpin balamut pytest-fast
$ rm -f $PMB_WORK/out/engine/*/balamut.r*.json $PMB_WORK/out/conformance/*/balamut.r1*.json
$ python3 -m pymutbench.pipeline generate --tools balamut --force --parallel 3
$ python3 -m pymutbench.pipeline judge --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline confirm --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline sensitivity --sample 1000   # when judge added verdicts
$ python3 -m pymutbench.pipeline prefix --parallel 2 --jobs 2 && python3 -m pymutbench.pipeline buggy
$ python3 -m pymutbench.pipeline prefix --parallel 2 --jobs 2
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline engine --reps 1
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline conformance
```

and the report commands above. `refresh-tool` tags every image that holds an environment of the tool (the
generation image, the trap image and the six project images) with `--measured`, unless that tag exists, and
rebuilds the image from it: the release named in `tools.toml` goes into the tool's existing environment, every
other installed distribution pinned to its version except those in `--unpin` (the tool's own and the
dependencies the release moves), and the command prints each environment's change. The other environments, the
reference one and the project trees stay as measured; a full rebuild would reinstall them all (with the versions
of `env/`, which `snapshot-env` records again afterwards). `judge` and `confirm` judge only the artifacts a union
did not have; `judge` also judges again an artifact the oracle gated out once a new origin lifts the gate. Every
verdict `judge` adds can change the stage 0/1 kills the oracle-shortcut sample is drawn from, and `report` checks
only that the recorded sample is judged: `sensitivity` draws it again from the artifacts of the current unions, and
a sampled mutant that already has a full-suite verdict keeps it. A project or trap union that gains artifacts is
judged again whole, which also renews the verdicts the other tools are compared with there.
