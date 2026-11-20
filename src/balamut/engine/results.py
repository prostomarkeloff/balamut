"""Verdicts of one mutation campaign and the errors that abort it."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from balamut.core.mutants import Mutant

type StatusCounts = dict[str, int]


class MutantStatus(Enum):
    SURVIVED = "survived"
    KILLED = "killed"
    TIMEOUT = "timeout"
    NO_COVERAGE = "no_coverage"
    UNSUPPORTED = "unsupported"


STATUS_VALUES = tuple(status.value for status in MutantStatus)


class VerdictReason(Enum):
    """Why a verdict is what it is: a kill obtained from a hang, or a timeout left unresolved."""

    NONE = ""
    # killed: a covering test hung on the mutant (which executed) and passed on the original in the same budget.
    HANG = "hang"
    # timeout: a hang without proven causality: the mutated code object never ran or the hung test is unknown.
    HANG_UNCONFIRMED = "hang_unconfirmed"
    # timeout: the same test hangs on the original too: the environment or the test, not the mutant.
    ORIGINAL_HANG = "original_hang"
    # timeout: the killer fails on the original too: an unstable test can neither kill nor prove survival.
    ORIGINAL_FAILED = "original_failed"


@dataclass(frozen=True, slots=True)
class MutantResult:
    mutant: Mutant
    status: MutantStatus
    covering: int
    # The content-addressed key (as in the store): it filters stale cache rows out of reports after in-place edits
    # of a file within one session. Empty for UNSUPPORTED results, which never reach a report.
    key: str = ""
    covering_tests: tuple[str, ...] = ()
    killed_by: tuple[str, ...] = ()
    reason: VerdictReason = VerdictReason.NONE


def status_counts(results: Iterable[MutantResult]) -> StatusCounts:
    counts = Counter(result.status.value for result in results)
    return {status: counts.get(status, 0) for status in STATUS_VALUES}


class CampaignSetupError(RuntimeError):
    """Preparing the resources of a mutation campaign failed."""


class CampaignExactnessError(RuntimeError):
    """The campaign could not prove an exact per-mutant verdict."""


class CampaignScopeChangedError(CampaignExactnessError):
    """Files of the pinned scope changed during the campaign: later verdicts would mix revisions."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__(f"the scope changed during the campaign: {', '.join(paths)}")
        self.paths = paths


class DaemonStaleError(RuntimeError):
    """A pytest-fast daemon refused a request: its environment or source fingerprint drifted from the client."""
