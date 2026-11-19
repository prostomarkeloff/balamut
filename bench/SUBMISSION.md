# Evaluating a tool from its own mutants

A tool that is not in the harness gets a PyMutBench result without any change to the harness's code: its author
runs the tool's generator on the exported inputs and submits the mutants; the harness judges them exactly as it
judges the benchmarked tools' mutants and writes the tool's card.

## 1. Inputs

```console
$ export PMB_WORK=~/pymutbench
$ python3 -m pymutbench.restore --results results          # the published release, once
$ python3 -m pymutbench.submit export-inputs --out inputs
```

Per bug, `inputs/<bug-id>/` holds:

| path | what it is |
|---|---|
| `fixed/<path>` | the files to mutate: the fixed program's source files the fix changed |
| `buggy/<path>` | the same files before the fix (for the third condition, below) |
| `scope.json` | the scope of the bug in the fixed files: `{"path": [[start, end, kind, name], ...]}` |
| `buggy_scope.json` | the same in the buggy files |
| `tree.json` | the repository, the fix commit and its parent: the whole tree, for a generator that needs the package (the parent commit with `files` taken from the commit) |

A bug id is `<project>@<commit12>`; it never changes, and a bug withdrawn from the corpus is marked, never
renumbered.

## 2. Mutants

Run the tool's own generator with its default configuration on the whole of each file in `fixed/` and write
`<bug-id>.jsonl`: one JSON object per native mutant, as [`mutant.schema.json`](mutant.schema.json) describes.

```json
{"path": "src/semver/version.py", "native_id": "m17", "operator": "ROR", "status": "ok", "mutated": "<the complete mutated file>"}
{"path": "src/semver/version.py", "native_id": "m18", "operator": "decorator", "status": "no_source_form", "line": 211}
```

* Every mutant the tool generates is a record. A mutant that has no faithful plain-source form keeps a `status`
  that says why and the `line` it changes: it counts as an emission (it costs the tool a run) and is never judged.
* The harness keeps the mutants whose edit lies inside the scope; the others are accounted for and dropped.
  Generating for the whole file, as the tool does for a user, keeps the comparison fair.
* For the third condition (a witness exists before the fix), write the same for the files in `buggy/` into a
  second directory. Without it the tool's witnesses on lines the fix rewrote stay undecided.

## 3. Evaluation

```console
$ python3 -m pymutbench.submit evaluate --tool NAME --version V --mutants mutants --buggy-mutants buggy-mutants --judge
```

`evaluate` normalizes the records (one byte-precise edit per mutated file, the scope filter, the accounting),
merges them into each bug's union of artifacts and, with `--judge`, runs the reference oracle on every artifact no
benchmarked tool has, re-judges every new witness from fresh trees and judges the buggy-side counterparts. Judging
needs the subjects' images and clones (`bench/README.md`); an artifact identical to one a benchmarked tool emitted
is judged already and costs nothing, so the cost is that of the new mutated files. What the artifacts only one
published tool emitted cost to judge is in `results/tables.md` (Cost and the Lite subset).

The card, `cards/NAME.json`, holds the tool's rank interval among the published tools, every measure with its
95% interval, and its paired difference from each published tool with a bootstrap p-value.

## Citing a result

A PyMutBench result names: the corpus version, the tool and its version, its configuration, the oracle's
semantics and code fingerprint, and the measure with its interval (for example, "PyMutBench corpus 1.0, results
2026.11, NAME 1.2 (default configuration), oracle bug/1: pre-fix detection 31% (95% CI 25–38%), rank 4–7 of 12").
A number without these fields is not a PyMutBench result.

## Submitting

Open a pull request with the card, the version and configuration, and the mutant files (or a link to them).
The results are published as a new release, and the maintainers of every benchmarked tool are asked to review
it first ([CONTRIBUTING.md](CONTRIBUTING.md)).
