"""Cross-judge normalization.

The model (JUDGING.md has the full argument):

    score[j, p] = quality[p] + leniency[j] + noise

    quality[p]   fixed effect, one per project, no prior
    leniency[j]  ~ Normal(0, var_judge)
    noise        ~ Normal(0, var_noise)

This is a linear mixed model with projects as fixed effects and judges as
random effects. var_judge and var_noise are estimated by restricted maximum
likelihood (REML); given them, quality and leniency come from Henderson's
mixed-model equations, in closed form, with their full covariance.

Why projects are fixed and judges random:

* Judge leniency is a nuisance to remove, and judges with one or two reviews
  cannot support a precise estimate of it. The prior shrinks a two-review
  judge toward "typical" and lets an eleven-review judge speak for themselves.
* Project quality is the thing being decided. Shrinking it would pull a
  project with two reviews toward the middle harder than one with five, which
  marks a team down for how many judges happened to see it. A fixed effect
  does not; the smaller evidence shows up as a wider interval instead.

Why not per-judge z-scores:

* A z-score divides by the judge's own standard deviation. A judge who gave
  every project the same score has a standard deviation of zero, and a judge
  with one review has none at all. The fixture data has both.
* A z-score assumes each judge saw a random slice of the field. Judges are
  assigned by track, so a judge who drew a strong track looks harsh to a
  z-score when they were simply right. The mixed model compares judges only
  through the projects they share with other judges.

This module is pure: no database, no web. It is exercised directly by the
tests and by tools/normalization_proof.py.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

VAR_FLOOR = 1e-9
Z90 = 1.6448536269514722
LOG_RATIO_BOUNDS = (-12.0, 12.0)


@dataclass(frozen=True)
class Observation:
    judge: str
    project: str
    score: float


@dataclass
class ProjectResult:
    project: str
    n_reviews: int
    raw_mean: float
    adjusted: float
    sd: float
    rank: int = 0
    raw_rank: int = 0
    p_above_next: float | None = None  # P(this project truly beats the one ranked below it)

    @property
    def low(self) -> float:
        return self.adjusted - Z90 * self.sd

    @property
    def high(self) -> float:
        return self.adjusted + Z90 * self.sd


@dataclass
class JudgeResult:
    judge: str
    n_reviews: int
    raw_mean: float
    leniency: float
    sd: float
    flags: list[str] = field(default_factory=list)


@dataclass
class Fit:
    var_judge: float
    var_noise: float
    evaluations: int
    n_observations: int
    components: int
    projects: list[ProjectResult]
    judges: list[JudgeResult]
    # False when there are no more reviews than projects: then nothing is
    # left over to estimate noise from, and no interval can be honest.
    identifiable: bool = True
    _index: dict = field(default_factory=dict, repr=False)
    _cov: object = field(default=None, repr=False)       # posterior covariance of true quality
    _mean: object = field(default=None, repr=False)      # posterior mean of true quality

    def p_better(self, a: str, b: str) -> float | None:
        """Probability that a's true quality exceeds b's (see _posterior)."""
        if not self.identifiable or self._cov is None:
            return None
        return _p_better(self._mean, self._cov, self._index[a], self._index[b])

    @property
    def judge_sd(self) -> float:
        return math.sqrt(self.var_judge) if self.identifiable else math.nan

    @property
    def noise_sd(self) -> float:
        return math.sqrt(self.var_noise) if self.identifiable else math.nan

    @property
    def spread_sd(self) -> float:
        """Estimated true spread of project quality, net of estimation noise."""
        if len(self.projects) < 2 or not self.identifiable:
            return 0.0
        adj = np.array([p.adjusted for p in self.projects])
        noise = float(np.mean([p.sd**2 for p in self.projects]))
        return math.sqrt(max(float(np.var(adj, ddof=1)) - noise, 0.0))

    def project(self, project_id: str) -> ProjectResult:
        return next(p for p in self.projects if p.project == project_id)

    def judge(self, judge_id: str) -> JudgeResult:
        return next(j for j in self.judges if j.judge == judge_id)


def combine(values: dict[str, float], criteria: list[dict]) -> float | None:
    """Weighted rubric score on a 0-100 scale.

    Each criterion is first mapped onto 0..1 using its own min and max, so a
    1-10 criterion and a 1-5 criterion weigh what their weights say and not
    what their ranges say. Returns None when the review is missing a
    criterion that carries weight.
    """
    total_weight = 0.0
    acc = 0.0
    for c in criteria:
        w = float(c["weight"])
        if w <= 0:
            continue
        if c["key"] not in values:
            return None
        lo, hi = float(c["min_value"]), float(c["max_value"])
        acc += w * (float(values[c["key"]]) - lo) / (hi - lo)
        total_weight += w
    if total_weight == 0:
        return None
    return 100.0 * acc / total_weight


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _p_better(mean, cov, i: int, j: int) -> float:
    var = cov[i, i] + cov[j, j] - 2 * cov[i, j]
    diff = mean[i] - mean[j]
    if var <= VAR_FLOOR:
        return 0.5 if abs(diff) < 1e-9 else float(diff > 0)
    return _phi(diff / math.sqrt(var))


def _posterior(q_hat: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Empirical-Bayes posterior of true project quality, for probabilities only.

    The adjusted scores are fixed-effect estimates, deliberately not shrunk,
    so a team is not marked down for how many judges saw it. But the chance
    that one project truly beats another must account for how alike projects
    are: when true differences are small next to the noise, a gap between two
    estimates is mostly noise, and a flat-prior probability overstates it
    (the calibration check in tools/normalization_proof.py measured stated
    84% against 60% observed on fixture-like data). So probabilities use a
    Normal(m, spread^2) prior on quality, with the spread estimated from the
    data itself (the variance of the estimates minus their average
    estimation variance), which is standard empirical Bayes.
    """
    n = len(q_hat)
    if n < 2:
        return q_hat.copy(), cov.copy(), 0.0
    spread2 = max(float(np.var(q_hat, ddof=1)) - float(np.mean(np.diag(cov))), 1e-6 * float(np.mean(np.diag(cov))))
    prec = np.linalg.inv(cov)
    post_cov = np.linalg.inv(prec + np.eye(n) / spread2)
    m = float(np.mean(q_hat))
    post_mean = post_cov @ (prec @ q_hat + m / spread2)
    return post_mean, post_cov, spread2


def _components(obs: list[Observation]) -> int:
    """Connected pieces of the judge-project graph.

    Leniency is only comparable within a piece. More than one piece means
    some pairs of projects share no chain of judges at all.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for o in obs:
        a, b = find("j:" + o.judge), find("p:" + o.project)
        if a != b:
            parent[a] = b
    return len({find(x) for x in list(parent)})


class _Design:
    def __init__(self, obs: list[Observation]):
        self.projects = sorted({o.project for o in obs})
        self.judges = sorted({o.judge for o in obs})
        self.pi = {p: i for i, p in enumerate(self.projects)}
        self.ji = {j: i for i, j in enumerate(self.judges)}
        self.P, self.J, self.n = len(self.projects), len(self.judges), len(obs)
        X = np.zeros((self.n, self.P + self.J))
        y = np.empty(self.n)
        for row, o in enumerate(obs):
            X[row, self.pi[o.project]] = 1.0
            X[row, self.P + self.ji[o.judge]] = 1.0
            y[row] = o.score
        self.X, self.y = X, y
        self.XtX = X.T @ X
        self.Xty = X.T @ y

    def solve(self, log_ratio: float, *, with_inverse: bool = False):
        """Mixed-model equations with var_noise / var_judge = exp(log_ratio).

        Returns (-2 * restricted log-likelihood up to a constant, solution,
        inverse coefficient matrix or None, var_noise). The inverse is only
        formed when asked for; the likelihood search does not need it.
        """
        lam = math.exp(log_ratio)
        A = self.XtX.copy()
        A[self.P :, self.P :] += lam * np.eye(self.J)
        L = np.linalg.cholesky(A)
        logdet = 2.0 * float(np.sum(np.log(np.diag(L))))
        m = np.linalg.solve(A, self.Xty)
        A_inv = np.linalg.inv(A) if with_inverse else None
        dof = self.n - self.P
        if dof <= 0:
            # One review per project: leniency and quality cannot be told apart.
            return math.inf, m, A_inv, VAR_FLOOR
        var_e = max(float(self.y @ (self.y - self.X @ m)) / dof, VAR_FLOOR)
        return dof * math.log(var_e) - self.J * log_ratio + logdet, m, A_inv, var_e


def _minimize_1d(f, lo: float, hi: float, grid: int = 25, tol: float = 1e-5) -> tuple[float, int]:
    """Coarse grid, then golden-section search around the best grid point."""
    xs = np.linspace(lo, hi, grid)
    vals = [f(x) for x in xs]
    evals = grid
    i = int(np.argmin(vals))
    if not math.isfinite(vals[i]):
        return hi, evals
    a, b = float(xs[max(i - 1, 0)]), float(xs[min(i + 1, grid - 1)])
    g = (math.sqrt(5) - 1) / 2
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    evals += 2
    while b - a > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
        evals += 1
    best = (a + b) / 2
    if vals[i] < f(best):
        best = float(xs[i])
    return best, evals + 1


def fit(observations: list[Observation]) -> Fit:
    obs = list(observations)
    if not obs:
        return Fit(0.0, 0.0, 0, 0, 0, [], [])
    d = _Design(obs)
    identifiable = d.n > d.P

    log_ratio, evals = _minimize_1d(lambda t: d.solve(t)[0], *LOG_RATIO_BOUNDS)
    _, m, A_inv, var_e = d.solve(log_ratio, with_inverse=True)
    var_b = var_e / math.exp(log_ratio)
    C = var_e * A_inv if identifiable else np.full_like(A_inv, math.inf)

    by_project: dict[str, list[float]] = defaultdict(list)
    by_judge: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        by_project[o.project].append(o.score)
        by_judge[o.judge].append(o.score)

    presults = [
        ProjectResult(
            project=p,
            n_reviews=len(by_project[p]),
            raw_mean=float(np.mean(by_project[p])),
            adjusted=float(m[d.pi[p]]),
            sd=math.sqrt(max(float(C[d.pi[p], d.pi[p]]), 0.0)),
        )
        for p in d.projects
    ]
    presults.sort(key=lambda r: (-r.adjusted, r.project))
    for rank, r in enumerate(presults, start=1):
        r.rank = rank
    for rank, r in enumerate(sorted(presults, key=lambda r: (-r.raw_mean, r.project)), start=1):
        r.raw_rank = rank
    post_mean = post_cov = None
    if identifiable:
        q_hat = np.array([float(m[d.pi[p]]) for p in d.projects])
        post_mean, post_cov, _ = _posterior(q_hat, C[: d.P, : d.P])
    for upper, lower in zip(presults, presults[1:]):
        upper.p_above_next = (_p_better(post_mean, post_cov, d.pi[upper.project], d.pi[lower.project])
                              if identifiable else None)

    jresults = []
    for j in d.judges:
        idx = d.P + d.ji[j]
        scores = by_judge[j]
        lean = float(m[idx])
        sd = math.sqrt(max(float(C[idx, idx]), 0.0))
        flags = []
        if not identifiable:
            flags.append("few-reviews")
            jresults.append(JudgeResult(judge=j, n_reviews=len(scores), raw_mean=float(np.mean(scores)),
                                        leniency=lean, sd=math.inf, flags=flags))
            continue
        if len(scores) >= 2 and max(scores) - min(scores) < 1e-9:
            flags.append("flat")  # gave every project the same score
        if len(scores) < 3:
            flags.append("few-reviews")  # leniency leans on the prior
        if sd > 0 and lean / sd > Z90:
            flags.append("generous")
        elif sd > 0 and lean / sd < -Z90:
            flags.append("harsh")
        jresults.append(
            JudgeResult(judge=j, n_reviews=len(scores), raw_mean=float(np.mean(scores)), leniency=lean, sd=sd, flags=flags)
        )
    jresults.sort(key=lambda r: (r.leniency, r.judge))

    return Fit(
        var_judge=float(var_b),
        var_noise=float(var_e),
        evaluations=evals,
        n_observations=d.n,
        components=_components(obs),
        projects=presults,
        judges=jresults,
        identifiable=identifiable,
        _index=dict(d.pi),
        _cov=post_cov,
        _mean=post_mean,
    )


# --- Baselines, used by the proof and shown side by side in the UI --------


def raw_means(observations: list[Observation]) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for o in observations:
        acc[o.project].append(o.score)
    return {p: float(np.mean(v)) for p, v in acc.items()}


def zscore_means(observations: list[Observation]) -> dict[str, float]:
    """The textbook method: z-score within each judge, average per project.

    Judges with fewer than two reviews or zero spread cannot be z-scored;
    they are mapped to 0, which is the usual silent fallback and one reason
    this method is not the one Plumb uses.
    """
    by_judge: dict[str, list[float]] = defaultdict(list)
    for o in observations:
        by_judge[o.judge].append(o.score)
    stats = {j: (float(np.mean(v)), float(np.std(v))) for j, v in by_judge.items()}
    acc: dict[str, list[float]] = defaultdict(list)
    for o in observations:
        mean, sd = stats[o.judge]
        acc[o.project].append((o.score - mean) / sd if sd > 1e-9 else 0.0)
    return {p: float(np.mean(v)) for p, v in acc.items()}
