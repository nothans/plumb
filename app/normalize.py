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

Intervals use Student's t with the residual degrees of freedom (reviews
minus projects), not the normal curve: the variances are estimated, and on a
small event that matters.

Probabilities ("how sure is it that A truly beats B") also need to know how
far apart projects truly are, because when true differences are small next to
the noise, a gap between two estimates is mostly noise. That spread is
estimated from its restricted likelihood, and every probability is averaged
over the spread's whole likelihood rather than read off one best guess: on
data that cannot tell a spread of 0 from a spread of 6, the probability says
so, instead of collapsing to 50% (spread 0) or overstating (spread 6).

This module is pure: no database, no web. It is exercised directly by the
tests and by tools/normalization_proof.py.
"""

from __future__ import annotations

import functools
import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

VAR_FLOOR = 1e-9
Z90 = 1.6448536269514722
LOG_RATIO_BOUNDS = (-12.0, 12.0)
SPREAD_GRID = 81       # points on the spread grid the probabilities are averaged over
RATIO_GRID = 9         # points on the judge-to-noise ratio grid
# Below this many residual degrees of freedom Plumb states no probabilities.
# In simulated events they ran high there (a stated 89% came true 54 to 75%
# of the time at 1 to 5 dof, a stated 94% 64 to 90% at 10), and no prior on
# the spread fixed it; from about 25 they matched (docs/normalization-proof.md,
# Part 3). The intervals stay honest at any dof, so the ranking and its
# intervals are still shown and can be published.
MIN_DOF = 20


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
    sd: float | None             # None when there are too few reviews to say
    rank: int = 0
    raw_rank: int = 0
    p_above_next: float | None = None  # P(this project truly beats the one ranked below it)
    q90: float = Z90                   # the two-sided 90% multiplier (Student's t)

    @property
    def low(self) -> float | None:
        return None if self.sd is None else self.adjusted - self.q90 * self.sd

    @property
    def high(self) -> float | None:
        return None if self.sd is None else self.adjusted + self.q90 * self.sd


@dataclass
class JudgeResult:
    judge: str
    n_reviews: int
    raw_mean: float
    leniency: float
    sd: float | None
    flags: list[str] = field(default_factory=list)


@dataclass
class Spread:
    """How far apart projects truly are, net of estimation noise, as a
    posterior over a grid of standard deviations (flat prior on [0, max])."""
    grid: np.ndarray
    weights: np.ndarray
    reml: float

    def quantile(self, q: float) -> float:
        return float(np.interp(q, np.cumsum(self.weights), self.grid))

    @property
    def mean(self) -> float:
        return float(self.weights @ self.grid)

    @property
    def zero_plausible(self) -> bool:
        """The data cannot rule out that every project is equally good: the
        likelihood at zero spread is within a likelihood ratio of 3.87
        (chi-square 1 df at 95%, halved for a boundary) of the best."""
        return bool(self.weights[0] / self.weights.max() > math.exp(-2.71 / 2))


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
    dof: int = 0                       # residual degrees of freedom: reviews minus projects
    spread: Spread | None = None
    _post: object = field(default=None, repr=False)

    def p_better(self, a: str, b: str, *, force: bool = False) -> float | None:
        """Probability that a's true quality exceeds b's, averaged over the
        spread's likelihood (see _Posterior). None when there are too few
        reviews to state one honestly (see MIN_DOF); force computes it anyway,
        which the proof uses to show why."""
        if self._post is None or (self.few_reviews and not force):
            return None
        return self._post.p_better(a, b)

    @property
    def judge_sd(self) -> float | None:
        return math.sqrt(self.var_judge) if self.identifiable else None

    @property
    def noise_sd(self) -> float | None:
        return math.sqrt(self.var_noise) if self.identifiable else None

    @property
    def spread_sd(self) -> float | None:
        """The best single estimate (REML) of the true spread of project quality."""
        return self.spread.reml if self.spread is not None else None

    @property
    def spread_summary(self) -> dict | None:
        """The spread in plain numbers: best estimate, a 90% range, and whether
        the data can rule out that all projects are equally good."""
        if self.spread is None:
            return None
        return {"estimate": round(self.spread.reml, 2), "low90": round(self.spread.quantile(0.05), 2),
                "high90": round(self.spread.quantile(0.95), 2), "zero_plausible": self.spread.zero_plausible}

    @property
    def few_reviews(self) -> bool:
        """Too few reviews beyond one per project for Plumb to state
        probabilities (see MIN_DOF)."""
        return self.identifiable and self.dof < MIN_DOF

    @property
    def barely_separated(self) -> bool:
        """The reviews cannot rule out that every project is equally good."""
        return self.spread is not None and self.spread.zero_plausible

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


# --- Student's t, without scipy --------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, dof: float) -> float:
    x = dof / (dof + t * t)
    tail = 0.5 * _betai(dof / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, dof: float) -> float:
    """The p quantile of Student's t with dof degrees of freedom."""
    if dof <= 0:
        return math.inf
    lo, hi = -1e3, 1e3
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, dof) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


@functools.lru_cache(maxsize=64)
def _t_table(dof: int) -> tuple[np.ndarray, np.ndarray]:
    """The t CDF on z >= 0, tabulated once per degrees of freedom."""
    z = np.linspace(0.0, 60.0, 3001)
    return z, np.array([t_cdf(float(x), dof) for x in z])


# --- the spread of true quality, and probabilities ---------------------------------


class _Component:
    """True project quality given the fixed-effect estimates, for one value
    of the judge-to-noise variance ratio.

    The estimates q_hat ~ Normal(q, C), with C their covariance from the
    mixed model. True quality q ~ Normal(mu, s^2 I), with s the spread. With
    one eigendecomposition C = U diag(lam) U', both the likelihood of s and
    the posterior of any difference q_a - q_b cost O(P) per value of s, so s
    can be integrated out on a grid instead of fixed at one estimate.
    """

    def __init__(self, q_hat: np.ndarray, cov: np.ndarray):
        cov = (cov + cov.T) / 2.0
        lam, U = np.linalg.eigh(cov)
        self.lam = np.maximum(lam, VAR_FLOOR)
        self.U = U
        self.uq = U.T @ q_hat
        self.u1 = U.T @ np.ones(len(q_hat))
        self.scale = max(float(np.std(q_hat, ddof=1)) if len(q_hat) > 1 else 0.0, math.sqrt(float(np.mean(self.lam))))

    def m2l(self, s: float) -> tuple[float, float]:
        """-2 x log density of q_hat given the spread s, with mu integrated
        out under a flat prior (so: restricted), and mu-hat."""
        w = 1.0 / (self.lam + s * s)
        w11 = float(np.sum(self.u1 * self.u1 * w))
        mu = float(np.sum(self.u1 * self.uq * w)) / w11
        r = self.uq - mu * self.u1
        return float(np.sum(np.log(self.lam + s * s)) + math.log(w11) + np.sum(r * r * w)), mu

    def on_grid(self, grid: np.ndarray) -> np.ndarray:
        """Precompute the per-s pieces the probabilities need; returns -2 log L(s)."""
        out = [self.m2l(float(s)) for s in grid]
        s2 = (grid ** 2)[:, None]
        self.shrink = s2 / (self.lam[None, :] + s2)                    # K x P
        self.mu = np.array([mu for _, mu in out])                       # K
        self.var_factor = s2 * self.lam[None, :] / (self.lam[None, :] + s2)
        return np.array([v for v, _ in out])

    def z(self, ia: int, ib: int) -> np.ndarray:
        """Standardized posterior mean of q_a - q_b, per grid point (0 where it has no variance)."""
        dv = self.U[ia] - self.U[ib]
        mean = self.shrink @ (dv * self.uq) + (1.0 - self.shrink) @ (dv * self.u1) * self.mu
        var = self.var_factor @ (dv * dv)
        z = np.zeros(len(mean))
        ok = var > VAR_FLOOR
        z[ok] = mean[ok] / np.sqrt(var[ok])
        return z


class _Posterior:
    """Probabilities that one project truly beats another, averaged over
    what the data leave uncertain.

    Three things are estimated before any probability can be stated: how
    lenient judges vary (the judge-to-noise variance ratio), how noisy a
    single review is, and how far apart projects truly are (the spread s).
    On a small event none of them is pinned down, and plugging in best
    guesses makes the probabilities overconfident (measured: stated 83%,
    observed 56%, on ten projects with two reviews each). So:

    * the ratio and the spread are integrated over a grid, weighted by
      their joint likelihood, which factorizes exactly: the restricted
      likelihood of the ratio with projects fixed, times the density of the
      project estimates given the spread;
    * the review noise is integrated analytically, which turns each normal
      probability into Student's t with the residual degrees of freedom.
    """

    def __init__(self, design: _Design, log_ratio: float, index: dict[str, int]):
        self.index = index
        P = design.P
        dof = design.n - design.P
        # The ratio's profile, on a grid around its best value.
        coarse = np.linspace(*LOG_RATIO_BOUNDS, 97)
        prof = np.array([design.solve(float(t))[0] for t in coarse])
        keep = coarse[prof - prof.min() < 2 * math.log(1e3)]
        lo, hi = float(min(keep.min(), log_ratio)), float(max(keep.max(), log_ratio))
        ratios = sorted(set(np.linspace(lo, hi, RATIO_GRID).tolist()) | {log_ratio}) if hi > lo else [log_ratio]
        comps, m2l1 = [], []
        for t in ratios:
            v, m, A_inv, var_e = design.solve(t, with_inverse=True)
            comps.append(_Component(m[:P], (var_e * A_inv)[:P, :P]))
            m2l1.append(v)
        best = comps[ratios.index(log_ratio)]
        # One spread grid for every component, wide enough for the widest.
        top = 4.0 * max(c.scale for c in comps) + 1.0
        for _ in range(6):
            grid = np.linspace(0.0, top, SPREAD_GRID)
            if best.m2l(top)[0] - min(best.m2l(float(s))[0] for s in grid[::8]) > 2 * math.log(1e5):
                break
            top *= 2.0
        m2l = np.array([m1 + c.on_grid(grid) for m1, c in zip(m2l1, comps)])   # ratios x spreads
        w = np.exp(-(m2l - m2l.min()) / 2.0)
        self.weights = w / w.sum()
        self.comps, self.grid = comps, grid
        # The t distribution with the residual degrees of freedom, tabulated once.
        self._table = _t_table(dof) if 0 < dof < 400 else None
        # The best single estimate of the spread: its restricted likelihood at the best ratio.
        cur = best.on_grid(grid)
        i = int(np.argmin(cur))
        reml, _ = _minimize_1d(lambda s: best.m2l(s)[0], float(grid[max(i - 1, 0)]),
                               float(grid[min(i + 1, len(grid) - 1)]), grid=9, tol=1e-6)
        self.reml = max(float(reml), 0.0)

    def _cdf(self, z: np.ndarray) -> np.ndarray:
        if self._table is None:
            return np.array([_phi(float(x)) for x in z.ravel()]).reshape(z.shape)
        upper = np.interp(np.abs(z), *self._table)     # the t distribution is symmetric
        return np.where(z >= 0, upper, 1.0 - upper)

    def p_better(self, a: str, b: str) -> float:
        ia, ib = self.index[a], self.index[b]
        z = np.array([c.z(ia, ib) for c in self.comps])
        return float(np.sum(self.weights * self._cdf(z)))

    def spread(self) -> Spread:
        return Spread(grid=self.grid, weights=self.weights.sum(axis=0), reml=self.reml)


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
        return Fit(0.0, 0.0, 0, 0, 0, [], [], identifiable=False)
    d = _Design(obs)
    dof = d.n - d.P
    identifiable = dof > 0

    log_ratio, evals = _minimize_1d(lambda t: d.solve(t)[0], *LOG_RATIO_BOUNDS)
    _, m, A_inv, var_e = d.solve(log_ratio, with_inverse=True)
    var_b = var_e / math.exp(log_ratio)
    C = var_e * A_inv if identifiable else None
    q90 = t_quantile(0.95, dof) if identifiable else math.inf

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
            sd=math.sqrt(max(float(C[d.pi[p], d.pi[p]]), 0.0)) if identifiable else None,
            q90=q90,
        )
        for p in d.projects
    ]
    # Ties in the estimate (two projects with the same reviews from the same
    # judges) are broken by id, never by floating-point noise, so the order is
    # the same on every run and in every export.
    presults.sort(key=lambda r: (-round(r.adjusted, 9), r.project))
    for rank, r in enumerate(presults, start=1):
        r.rank = rank
    for rank, r in enumerate(sorted(presults, key=lambda r: (-round(r.raw_mean, 9), r.project)), start=1):
        r.raw_rank = rank

    post = None
    if identifiable and d.P >= 2:
        post = _Posterior(d, log_ratio, dict(d.pi))
        if dof >= MIN_DOF:
            for upper, lower in zip(presults, presults[1:]):
                upper.p_above_next = post.p_better(upper.project, lower.project)

    jresults = []
    for j in d.judges:
        idx = d.P + d.ji[j]
        scores = by_judge[j]
        lean = float(m[idx])
        flags = []
        if not identifiable:
            flags.append("few-reviews")
            jresults.append(JudgeResult(judge=j, n_reviews=len(scores), raw_mean=float(np.mean(scores)),
                                        leniency=lean, sd=None, flags=flags))
            continue
        sd = math.sqrt(max(float(C[idx, idx]), 0.0))
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
        dof=max(dof, 0),
        spread=post.spread() if post is not None else None,
        _post=post,
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
