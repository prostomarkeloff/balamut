"""Statistics of the benchmark: intervals and paired tests over bugs nested in projects (stdlib only).

* ``wilson`` — the Wilson score interval of a binomial proportion;
* ``cluster_bootstrap`` — a percentile interval that resamples whole projects (bugs of one project are not
  independent), with a fixed seed;
* ``Bootstrap`` — the same resampling shared by many statistics: percentile and BCa intervals, intervals of ranks,
  paired differences with bootstrap p-values;
* ``holm`` — Holm's step-down adjustment of a family of p-values;
* ``split_half`` — the rank reliability of an ordering of tools: the Spearman correlation of the tools' values
  between two random halves of the projects, median over many splits.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from statistics import NormalDist, median

SEED = 20261105
REPLICATES = 10000


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cluster_bootstrap(
    clusters: dict[str, list[float]],
    statistic: Callable[[dict[str, list[float]]], float],
    *,
    replicates: int = 10000,
    seed: int = 20261105,
    alpha: float = 0.05,
) -> tuple[float, float]:
    names = sorted(clusters)
    if not names:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    values = []
    for _ in range(replicates):
        sample: dict[str, list[float]] = {}
        for index in range(len(names)):
            name = rng.choice(names)
            sample[f"{name}#{index}"] = clusters[name]
        values.append(statistic(sample))
    values.sort()
    low = values[math.floor(alpha / 2 * replicates)]
    high = values[min(replicates - 1, math.ceil((1 - alpha / 2) * replicates) - 1)]
    return (low, high)


def pooled_mean(clusters: dict[str, list[float]]) -> float:
    flat = [v for values in clusters.values() for v in values]
    return sum(flat) / len(flat) if flat else math.nan


def project_mean(clusters: dict[str, list[float]]) -> float:
    """The mean of per-project means: every project weighs the same, however many bugs it contributes."""
    means = [sum(v) / len(v) for v in clusters.values() if v]
    return sum(means) / len(means) if means else math.nan


def _percentile_pair(values: list[float], alpha: float) -> tuple[float, float]:
    ordered = sorted(values)
    n = len(ordered)
    return (ordered[math.floor(alpha / 2 * n)], ordered[min(n - 1, math.ceil((1 - alpha / 2) * n) - 1)])


class Bootstrap:
    """Projects resampled with replacement ``replicates`` times from one seed: every statistic computed on the same
    resamples, so intervals of ranks and paired differences use one joint distribution. The draws are those of
    ``cluster_bootstrap`` with the same seed, so a pooled mean gets the same percentile interval from both.

    A statistic is a pooled mean over bugs: per project the sum of a bug-level value and the number of bugs."""

    def __init__(self, projects: Sequence[str], *, replicates: int = REPLICATES, seed: int = SEED) -> None:
        self.names = sorted(projects)
        rng = random.Random(seed)
        index = {name: i for i, name in enumerate(self.names)}
        self.draws: list[list[int]] = []  # per replicate: how often each project was drawn
        for _ in range(replicates):
            counts = [0] * len(self.names)
            for _ in range(len(self.names)):
                counts[index[rng.choice(self.names)]] += 1
            self.draws.append(counts)

    def replicates(self, clusters: dict[str, list[float]]) -> list[float]:
        sums = [sum(clusters.get(name, [])) for name in self.names]
        sizes = [len(clusters.get(name, [])) for name in self.names]
        out = []
        for counts in self.draws:
            n = sum(c * s for c, s in zip(counts, sizes, strict=True))
            out.append(sum(c * s for c, s in zip(counts, sums, strict=True)) / n if n else math.nan)
        return out

    def percentile(self, clusters: dict[str, list[float]], alpha: float = 0.05) -> tuple[float, float]:
        return _percentile_pair(self.replicates(clusters), alpha)

    def bca(self, clusters: dict[str, list[float]], alpha: float = 0.05) -> tuple[float, float]:
        """The bias-corrected and accelerated interval (Efron 1987): the bias from the share of replicates below
        the estimate, the acceleration from the leave-one-project-out jackknife."""
        values = sorted(self.replicates(clusters))
        estimate = pooled_mean(clusters)
        below = sum(1 for v in values if v < estimate) + 0.5 * sum(1 for v in values if v == estimate)
        normal = NormalDist()
        share = min(max(below / len(values), 1 / len(values)), 1 - 1 / len(values))
        z0 = normal.inv_cdf(share)
        jack = [pooled_mean({p: v for p, v in clusters.items() if p != name}) for name in clusters]
        centre = sum(jack) / len(jack)
        num = sum((centre - j) ** 3 for j in jack)
        den = 6 * sum((centre - j) ** 2 for j in jack) ** 1.5
        accel = num / den if den else 0.0
        bounds = []
        for z in (normal.inv_cdf(alpha / 2), normal.inv_cdf(1 - alpha / 2)):
            adjusted = normal.cdf(z0 + (z0 + z) / (1 - accel * (z0 + z)))
            bounds.append(values[min(len(values) - 1, max(0, int(adjusted * len(values))))])
        return bounds[0], bounds[1]

    @staticmethod
    def interval(series: list[float], alpha: float = 0.05) -> tuple[float, float]:
        return _percentile_pair(series, alpha)

    @staticmethod
    def ranks(series: dict[str, list[float]], alpha: float = 0.05) -> dict[str, tuple[int, int]]:
        """Per tool the interval of its rank among the tools of ``series`` (replicate series of one statistic;
        1 = highest value, ties share the mean rank), widened to whole ranks."""
        tools = sorted(series)
        ranks: dict[str, list[float]] = {tool: [] for tool in tools}
        for i in range(len(series[tools[0]])):
            values = [series[tool][i] for tool in tools]
            for tool, value in zip(tools, values, strict=True):
                higher = sum(1 for other in values if other > value)
                tied = sum(1 for other in values if other == value)
                ranks[tool].append(higher + (tied + 1) / 2)
        out = {}
        for tool in tools:
            low, high = _percentile_pair(ranks[tool], alpha)
            out[tool] = (math.floor(low), math.ceil(high))
        return out

    @staticmethod
    def difference(first: list[float], second: list[float], estimate: float,
                   alpha: float = 0.05) -> dict[str, float | tuple[float, float]]:
        """The difference of two pooled means over the same bugs, from their replicate series: the estimate, its
        percentile interval and the two-sided bootstrap p-value (twice the smaller share of replicates on either
        side of zero, at least one over the number of replicates)."""
        values = [a - b for a, b in zip(first, second, strict=True)]
        low_side = sum(1 for v in values if v <= 0) / len(values)
        high_side = sum(1 for v in values if v >= 0) / len(values)
        p = min(1.0, max(1 / len(values), 2 * min(low_side, high_side)))
        return {"difference": estimate, "ci": _percentile_pair(values, alpha), "p": p}


def spearman(first: Sequence[float], second: Sequence[float]) -> float:
    def ranked(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    a, b = ranked(first), ranked(second)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return cov / var if var else math.nan


def split_half(by_tool: dict[str, dict[str, list[float]]], *, splits: int = 2000, seed: int = SEED) -> float:
    """The median over ``splits`` random halvings of the projects of the Spearman correlation between the tools'
    pooled means on one half and on the other."""
    tools = sorted(by_tool)
    projects = sorted({p for clusters in by_tool.values() for p in clusters})
    rng = random.Random(seed)
    values = []
    for _ in range(splits):
        half = set(rng.sample(projects, len(projects) // 2))
        first = [pooled_mean({p: v for p, v in by_tool[t].items() if p in half}) for t in tools]
        second = [pooled_mean({p: v for p, v in by_tool[t].items() if p not in half}) for t in tools]
        rho = spearman(first, second)
        if not math.isnan(rho):
            values.append(rho)
    return median(values) if values else math.nan


def holm(pvalues: Sequence[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    m = len(pvalues)
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def hit_probability(population: int, hits: int, draws: int) -> float:
    """P(a uniform sample of ``draws`` of ``population`` items contains at least one of ``hits``)."""
    if population <= 0 or hits <= 0:
        return 0.0
    draws = min(draws, population)
    if population - hits < draws:
        return 1.0
    return 1.0 - math.comb(population - hits, draws) / math.comb(population, draws)
