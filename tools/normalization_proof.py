#!/usr/bin/env python3
"""The normalization proof: does Plumb's method recover the truth better
than the alternatives, on the exact judging design of the DOGFOOD fixtures?

We cannot know the true quality of the fixture projects, so we plant one.
Keep the fixture's real judge-project graph (which judge saw which project,
unbalanced and all), invent true project qualities and judge biases, draw
scores from them, and ask each method to recover the planted ranking.

Scenarios:
  additive     judges differ by a constant lean (the model's assumption)
  scale        judges also differ in how spread out their scores are
  flat         as additive, plus one judge who gives everyone the same score
               (the fixture has one: jdg_07)
  track        tracks differ in true quality and judges stay in their tracks,
               as the fixture judges do: the case JUDGING.md argues breaks
               per-judge z-scores
  discrete     as additive, but every review is three whole-number 1-5
               criteria (with the floor and ceiling that implies), combined
               the way Plumb combines a rubric
  no-bias      judges are all fair; checks the correction costs nothing

Methods:
  raw          plain average of each project's scores
  zscore       per-judge z-scores, then averaged (the textbook method)
  plumb        projects fixed, judges random, REML (app/normalize.py)
  pairwise     Bradley-Terry on the preferences implied within each judge's
               scores (app/pairwise.py); ignores every judge's scale

Metrics, averaged over many draws (with the paired difference from raw
means and its 95% Monte Carlo interval, so a small gap can be told from luck):
  rmse         error of the estimated quality (after centering)
  spearman     rank correlation with the truth
  top5         how many of the true top 5 each method puts in its top 5
  coverage90   share of projects whose 90% interval contains the truth
               (plumb only; the others do not give intervals)

    python tools/normalization_proof.py [--draws 1000] [--seed 7] [--out docs/normalization-proof.md]

Writes a Markdown report; JUDGING.md quotes it, and tests/test_proof.py
re-runs a small version to keep the headline ordering honest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import the app package before numpy: it pins BLAS to one thread, which
# only takes effect if it happens before numpy loads.
from app import pairwise  # noqa: E402
from app.normalize import Observation, combine, fit, raw_means, zscore_means  # noqa: E402

import numpy as np  # noqa: E402


def fixture() -> dict:
    return json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))


def design(fx: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
    pairs = [(s["judge"], s["project"]) for s in fx["scores"] if s["project"] != "prj_41"]
    track = {p["id"]: p["track"] for p in fx["projects"]}
    return pairs, track


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


SCENARIOS = ("additive", "scale", "flat", "track", "discrete", "no-bias")
METHODS = ("raw", "zscore", "plumb", "pairwise")
CRITERIA = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("a", "b", "c")]


def simulate(pairs, track_of, rng, scenario: str, q_sd=12.0, b_sd=8.0, e_sd=10.0):
    projects = sorted({p for _, p in pairs})
    judges = sorted({j for j, _ in pairs})
    truth = dict(zip(projects, rng.normal(50, q_sd, len(projects))))
    if scenario == "track":
        tracks = sorted(set(track_of[p] for p in projects))
        shift = dict(zip(tracks, rng.normal(0, 8.0, len(tracks))))
        truth = {p: v + shift[track_of[p]] for p, v in truth.items()}
    bias = dict(zip(judges, rng.normal(0, b_sd if scenario != "no-bias" else 0.0, len(judges))))
    scale = dict(zip(judges, np.exp(rng.normal(0, 0.35, len(judges))) if scenario == "scale" else np.ones(len(judges))))
    obs = []
    for j, p in pairs:
        if scenario == "flat" and j == "jdg_07":
            y = 70.0
        elif scenario == "discrete":
            base = truth[p] + bias[j]
            values = {c["key"]: int(np.clip(np.rint(1 + 4 * (base + rng.normal(0, e_sd * 1.4)) / 100), 1, 5))
                      for c in CRITERIA}
            y = combine(values, CRITERIA)
        else:
            y = 50 + scale[j] * (truth[p] - 50) + bias[j] + rng.normal(0, e_sd)
        obs.append(Observation(judge=j, project=p, score=float(y)))
    return projects, truth, obs


def evaluate(draws: int, seed: int, scenarios=SCENARIOS) -> dict:
    fx = fixture()
    pairs, track_of = design(fx)
    rng = np.random.default_rng(seed)
    out = {}
    for scenario in scenarios:
        acc = {m: {"rmse": [], "spearman": [], "top5": []} for m in METHODS}
        coverage = []
        for _ in range(draws):
            projects, truth, obs = simulate(pairs, track_of, rng, scenario)
            t = np.array([truth[p] for p in projects])
            f = fit(obs)
            est = {
                "raw": raw_means(obs),
                "zscore": zscore_means(obs),
                "plumb": {r.project: r.adjusted for r in f.projects},
                "pairwise": {r.project: r.strength for r in pairwise.fit(pairwise.implied(obs), projects).strengths},
            }
            for m, e in est.items():
                v = np.array([e[p] for p in projects])
                if m in ("zscore", "pairwise"):
                    # These live on other scales; fit each to the truth's scale
                    # so rmse compares like with like (this flatters them).
                    v = np.polyval(np.polyfit(v, t, 1), v)
                acc[m]["rmse"].append(float(np.sqrt(np.mean(((v - v.mean()) - (t - t.mean())) ** 2))))
                acc[m]["spearman"].append(spearman(v, t))
                top_true = set(np.array(projects)[np.argsort(-t)[:5]])
                top_est = set(np.array(projects)[np.argsort(-v)[:5]])
                acc[m]["top5"].append(len(top_true & top_est))
            shift = np.mean([r.adjusted for r in f.projects]) - t.mean()
            coverage.append(np.mean([r.low - shift <= truth[r.project] <= r.high - shift for r in f.projects]))
        res = {}
        raw_sp = np.array(acc["raw"]["spearman"])
        for m, d in acc.items():
            diff = np.array(d["spearman"]) - raw_sp
            res[m] = {k: float(np.mean(v)) for k, v in d.items()}
            res[m]["vs_raw"] = float(diff.mean())
            res[m]["vs_raw_ci"] = float(1.96 * diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else 0.0
        res["plumb"]["coverage90"] = float(np.mean(coverage))
        out[scenario] = res
    return out


def report(res: dict, draws: int, seed: int) -> str:
    lines = [
        f"{draws} draws per scenario, seed {seed}, on the fixture's judge-project graph "
        "(122 reviews, 40 projects, 30 judges, the duplicate's reviews left out; "
        "true project sd 12, judge lean sd 8, noise sd 10).",
        "",
        "| scenario | method | rmse | spearman | vs raw (95% MC interval) | true top 5 found | 90% coverage |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for scenario, methods in res.items():
        for m, r in methods.items():
            cov = f"{r['coverage90']:.0%}" if "coverage90" in r else ""
            vs = "" if m == "raw" else f"{r['vs_raw']:+.3f} (±{r['vs_raw_ci']:.3f})"
            lines.append(f"| {scenario} | {m} | {r['rmse']:.2f} | {r['spearman']:.3f} | {vs} | {r['top5']:.2f} | {cov} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None, help="also write the report to this file")
    args = ap.parse_args()
    text = report(evaluate(args.draws, args.seed), args.draws, args.seed)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("# Normalization proof: output\n\nGenerated by `python tools/normalization_proof.py "
                                  f"--draws {args.draws} --seed {args.seed} --out {args.out}`.\n\n" + text,
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
