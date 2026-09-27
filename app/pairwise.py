"""Pairwise judging: a Bradley-Terry estimator.

    P(i beats j) = 1 / (1 + exp(-(s_i - s_j)))

Strengths s are fitted by penalized maximum likelihood (Newton's method) with
a Normal(0, PRIOR_SD^2) prior on each strength. The prior does two jobs: it
pins down the otherwise free overall level, and it keeps a project that won
(or lost) every comparison it was in at a finite strength instead of
infinity. Standard errors come from the inverse of the penalized observed
information, so a project seen in two comparisons gets a wide interval.

Comparisons come from two places, and both feed the same estimator:

* direct: a judge looks at two projects and picks the better one
  (Gavel-style). Plumb picks the next pair to show a judge actively: the pair
  among their assigned projects whose order is least certain.
* implied: every judge's rubric scores, read as "within this judge, A scored
  above B". This throws away each judge's scale entirely (harsh, generous,
  compressed and stretched judges all produce the same preferences), so it is
  a second estimator that makes no assumption about how judges use the
  scale. Ties count as half a win each way, so a judge who gave everything
  the same score contributes nothing, which is exactly right.

Pure: no database, no web.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

import numpy as np

from .normalize import Observation

PRIOR_SD = 2.0
Z90 = 1.6448536269514722


@dataclass(frozen=True)
class Comparison:
    winner: str
    loser: str
    weight: float = 1.0  # 0.5 each way for a tie


@dataclass
class Strength:
    project: str
    strength: float
    sd: float
    comparisons: float
    wins: float
    rank: int = 0

    @property
    def low(self) -> float:
        return self.strength - Z90 * self.sd

    @property
    def high(self) -> float:
        return self.strength + Z90 * self.sd


@dataclass
class BTFit:
    strengths: list[Strength]
    iterations: int
    comparisons: float
    _index: dict[str, int]
    _cov: np.ndarray
    _s: np.ndarray

    def get(self, project: str) -> Strength:
        return self.strengths[[s.project for s in self.strengths].index(project)]

    def p_beats(self, a: str, b: str) -> float:
        """Estimated probability that a would win a comparison with b."""
        i, j = self._index[a], self._index[b]
        return 1.0 / (1.0 + math.exp(-(self._s[i] - self._s[j])))

    def p_better(self, a: str, b: str) -> float:
        """Posterior probability that a's true strength exceeds b's."""
        i, j = self._index[a], self._index[b]
        var = self._cov[i, i] + self._cov[j, j] - 2 * self._cov[i, j]
        diff = self._s[i] - self._s[j]
        if var > 1e-12:
            return 0.5 * (1 + math.erf(diff / math.sqrt(2 * var)))
        return 0.5 if abs(diff) < 1e-12 else float(diff > 0)


def fit(comparisons: list[Comparison], projects: list[str] | None = None, *, max_iter: int = 100,
        tol: float = 1e-9) -> BTFit:
    names = sorted(set(projects or []) | {c.winner for c in comparisons} | {c.loser for c in comparisons})
    idx = {p: i for i, p in enumerate(names)}
    n = len(names)
    s = np.zeros(n)
    prec = 1.0 / PRIOR_SD**2
    w = np.array([c.weight for c in comparisons], dtype=float)
    wi = np.array([idx[c.winner] for c in comparisons], dtype=int)
    li = np.array([idx[c.loser] for c in comparisons], dtype=int)
    it = 0
    H = np.eye(n) * prec
    for it in range(1, max_iter + 1):
        d = s[wi] - s[li]
        p = 1.0 / (1.0 + np.exp(-d))           # P(winner beats loser) under current s
        g = -prec * s
        np.add.at(g, wi, w * (1 - p))
        np.add.at(g, li, -w * (1 - p))
        h = w * p * (1 - p)
        H = np.eye(n) * prec
        np.add.at(H, (wi, wi), h)
        np.add.at(H, (li, li), h)
        np.add.at(H, (wi, li), -h)
        np.add.at(H, (li, wi), -h)
        step = np.linalg.solve(H, g)
        s = s + step
        if float(np.max(np.abs(step))) < tol:
            break
    cov = np.linalg.inv(H) if n else np.zeros((0, 0))
    count = np.zeros(n)
    wins = np.zeros(n)
    np.add.at(count, wi, w)
    np.add.at(count, li, w)
    np.add.at(wins, wi, w)
    out = [Strength(project=p, strength=float(s[idx[p]]), sd=math.sqrt(max(float(cov[idx[p], idx[p]]), 0.0)),
                    comparisons=float(count[idx[p]]), wins=float(wins[idx[p]])) for p in names]
    out.sort(key=lambda r: (-r.strength, r.project))
    for rank, r in enumerate(out, start=1):
        r.rank = rank
    return BTFit(strengths=out, iterations=it, comparisons=float(w.sum()), _index=idx, _cov=cov, _s=s)


def implied(observations: list[Observation]) -> list[Comparison]:
    """Within-judge preferences from rubric scores. Scale-free by construction."""
    by_judge: dict[str, list[Observation]] = {}
    for o in observations:
        by_judge.setdefault(o.judge, []).append(o)
    out: list[Comparison] = []
    for obs in by_judge.values():
        for a, b in combinations(obs, 2):
            if abs(a.score - b.score) < 1e-9:
                out.append(Comparison(a.project, b.project, 0.5))
                out.append(Comparison(b.project, a.project, 0.5))
            elif a.score > b.score:
                out.append(Comparison(a.project, b.project))
            else:
                out.append(Comparison(b.project, a.project))
    return out


def next_pair(assigned: list[str], done: set[frozenset[str]], current: BTFit | None) -> tuple[str, str] | None:
    """The pair a judge should compare next, among their assigned projects.

    Never a pair this judge already compared. Otherwise the pair whose order
    is least certain (P(better) closest to 1/2), breaking ties toward
    projects with fewer comparisons so coverage stays even.
    """
    best, best_key = None, None
    for a, b in combinations(sorted(assigned), 2):
        if frozenset((a, b)) in done:
            continue
        if current is not None and a in current._index and b in current._index:
            certainty = abs(current.p_better(a, b) - 0.5)
            seen = current.get(a).comparisons + current.get(b).comparisons
        else:
            certainty, seen = 0.0, 0.0
        key = (certainty, seen, a, b)
        if best_key is None or key < best_key:
            best, best_key = (a, b), key
    return best
