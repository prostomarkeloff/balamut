# Contributing to PyMutBench

PyMutBench is maintained by the authors of balamut, one of the benchmarked tools. The procedures below keep its
results open to correction by every other tool's maintainers.

## Corrections and disputes

* A wrong adapter, campaign configuration or tool version is corrected by a pull request against `bench/`; a
  disputed result or protocol decision is raised as an issue. Both are answered in public.
* A corrected result is published as a new results release; the earlier release stays as it was.

## New tools and results

A tool not in the harness is evaluated from its own mutants (`SUBMISSION.md`) and submitted by a pull request with
its card. A tool joins the harness itself with a generation adapter, a campaign in `pymutbench/native.py` and an
entry in `pymutbench/tools.toml` (`README.md`, "Adding a tool"); its maintainers are asked to confirm the
configuration.

## The corpus

The corpus has a fixed core and an evolving part. A corpus version (`pymutbench.CORPUS`) never changes: its bugs,
their tests, labels and ids stay as published, and a bug found invalid is marked as withdrawn, not renumbered or
removed. New bugs go into a new corpus version, drawn by a predeclared rule, so no tool can be tuned to a fixed
set; results of different corpus versions are not compared.

## Code

The harness is stdlib-only Python. `python -m unittest discover -s bench/tests` runs its tests, and the CI job
checks that the report rebuilt from `results/data` equals the published one.
