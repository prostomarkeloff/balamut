"""Content fingerprints of the code that determines each stage's results, and the oracle's verdict semantics.

A generation ledger carries the fingerprint of the shared generation code and of its own tool's adapter; the
report refuses to aggregate a tool's ledgers whose fingerprints differ, so a resumed or partly repeated run can
never mix two versions of the code behind one tool's mutants.

An oracle record carries the version of its mode's verdict semantics and, as a reference, the fingerprint of the
files the oracle executes. An oracle run appends to a result file, and the report aggregates oracle results, only
when every record has the same semantics: a change of the oracle code that cannot change a mode's verdicts keeps
the mode's version, so judging a new artifact does not require judging the whole corpus again; a change that can
change them raises the version.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

GENERATION_SHARED = ("generate.py", "artifacts.py", "astmat.py", "scope.py", "gitops.py", "adapters/_common.py")
STAGES: dict[str, tuple[str, ...]] = {
    "oracle": ("oracle.py", "pytestrun.py", "plugin_path/pmb_pytest_plugin.py", "artifacts.py", "gitops.py",
               "validate.py", "subjects.py", "fingerprint.py"),
}

# The verdict semantics of each oracle mode: ``bug`` (the historical suite on the fixed source, then the fix's
# tests), ``parent`` (the historical suite on the buggy parent) and ``suite`` (a project's own suite at HEAD).
ORACLE_SEMANTICS: dict[str, int] = {"bug": 1, "parent": 1, "suite": 1}
# Records written before the version field existed name only their code. Each fingerprint below wrote verdicts of
# the version given: ecd9f95dcc8ee860 (the published bug-track verdicts) and 33a67b4e3a1f1650 differ from each
# other and from later code in the suite mode's test roots and meta record and in how a pytest run without
# arguments writes its argument file. A bug-mode run always has arguments (every bug has a test root and keeps a
# fail-to-pass test; single tests and covering subsets are never empty), so its verdicts do not depend on them. A
# re-judgement of a seeded sample of artifacts by later code checks the equivalence
# (``pipeline equivalence``, published as ``results/oracle_equivalence.json``).
LEGACY_ORACLE: dict[tuple[str, str], int] = {
    ("bug", "ecd9f95dcc8ee860"): 1,
    ("bug", "33a67b4e3a1f1650"): 1,
    ("suite", "33a67b4e3a1f1650"): 1,
}


def oracle_mode(meta: dict[str, Any]) -> str:
    return str(meta.get("mode", "bug"))


def oracle_semantics(meta: dict[str, Any]) -> str:
    """The semantics of an oracle meta record as ``mode/version``; an unknown legacy record names its code."""
    if "semantics" in meta:
        return str(meta["semantics"])
    mode = oracle_mode(meta)
    version = LEGACY_ORACLE.get((mode, str(meta.get("fingerprint", ""))))
    return f"{mode}/{version}" if version is not None else f"{mode}/unversioned:{meta.get('fingerprint', '?')}"


def current_semantics(mode: str) -> str:
    return f"{mode}/{ORACLE_SEMANTICS[mode]}"


def _digest(patterns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for pattern in patterns:
        for path in sorted(ROOT.glob(pattern)):
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def fingerprint(stage: str) -> str:
    return _digest(STAGES[stage])


def generation_fingerprint(adapter: str) -> str:
    """The code one tool's generation ledgers depend on: the shared generation files and that tool's adapter."""
    return _digest((*GENERATION_SHARED, f"adapters/{adapter}"))


def buggy_generation_fingerprint(adapter: str) -> str:
    """The same for the generation on the buggy files (``generate_buggy``)."""
    return _digest((*GENERATION_SHARED, "generate_buggy.py", f"adapters/{adapter}"))
