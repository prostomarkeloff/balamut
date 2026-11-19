"""PyMutBench: a reproducible benchmark of Python mutation-testing tools on CPython 3.14.

The harness is stdlib-only. Everything that executes third-party code (subject projects, their test suites
and the mutation tools themselves) runs inside the pinned container image described by ``bench/Dockerfile``.

Three versions name what a result rests on: the corpus (``CORPUS``: the frozen set of bugs, their tests and
labels; a bug's id ``<project>@<commit12>`` never changes and a withdrawn bug is marked, not renumbered), the
harness (``__version__``, semantic versioning) and the results release (``RESULTS``, year and month).
"""

__version__ = "0.2.0"
CORPUS = "1.0"
RESULTS = "2026.11"
