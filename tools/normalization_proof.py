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
  fixture      calibrated to what the REML fit finds in the real fixture
               reviews: judge lean sd 4.3, noise sd 15.9, and a small true
               spread (sd 4). The regime Plumb actually faces on this data.

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
  coverage90   share of projects whose 90% interval contains the truth,
               once the overall level is lined up (the ranking is relative)
  stated90     the same with the intervals exactly as stated, level and all,
               as a certificate prints them (plumb only; the others give none)
  brier        mean squared error of "beats next" against what happened
               (0.25 is a coin; lower is better)

    python tools/normalization_proof.py [--draws 1000] [--seed 7] [--out docs/normalization-proof.md]

Also checks calibration: across all draws, every adjacent pair in Plumb's
ranking gets a "beats next" probability; binned, the share of pairs whose
true order matches should equal the stated probability. Those numbers are
signed into results and certificates, so they must mean what they say.

Part 3 runs the same calibration check on small events (10 to 40
projects, 2 or 3 reviews each, random balanced designs) to find where the
probabilities stop being trustworthy: the basis of the "few reviews" note
(app/normalize.py MIN_DOF).

Writes a Markdown report; JUDGING.md quotes it, and tests/test_proof.py
re-runs a small version to keep the headline ordering honest.
"""

from __future__ import annotations

import os

# Pin BLAS to one thread before numpy can load: the model solves many tiny
# systems, and multithreaded BLAS makes that about 60x slower. (app/__init__
# does the same, but import sorting must not be able to undo it here.)
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from collections import defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app import pairwise  # noqa: E402
from app.normalize import MIN_DOF, Observation, combine, fit, raw_means, zscore_means  # noqa: E402


def judge_spread(scores, lean=None) -> float:
    """Sample standard deviation of per-judge mean scores on the 1-5 scale
    (a review's score is the mean of its criteria): the organizers' sigma.
    With lean, each judge's estimated lean (0-100 scale) is removed first."""
    import statistics
    per = defaultdict(list)
    for s in scores:
        v = sum(s["criteria"].values()) / len(s["criteria"])
        if lean is not None:
            v -= 4 * lean[s["judge"]] / 100
        per[s["judge"]].append(v)
    return statistics.stdev(statistics.mean(v) for v in per.values())


def fixture_section(fx: dict) -> str:
    """Part 1: the method run on the real fixtures, before and after."""
    crit = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("functionality", "quality", "innovation")]
    titles = {p["id"]: p["title"] for p in fx["projects"]}
    lines = ["## Part 1: the real fixture data", ""]
    for label, drop in (("all 126 reviews", None), ("121 reviews, the superseded duplicate prj_07 left out (what Plumb ranks)", "prj_07")):
        scores = [s for s in fx["scores"] if s["project"] != drop]
        f = fit([Observation(s["judge"], s["project"], combine(s["criteria"], crit)) for s in scores])
        lean = {j.judge: j.leniency for j in f.judges}
        lines.append(f"* {label}: per-judge spread (sample sd of judge means, 1-5 scale) "
                     f"**{judge_spread(scores):.2f} raw, {judge_spread(scores, lean):.2f} after removing each judge's "
                     f"estimated lean**; estimated true judge lean sd {4 * f.judge_sd / 100:.2f}, "
                     f"review noise sd {4 * f.noise_sd / 100:.2f}.")
    lines += [
        "",
        "The organizers' sigma (0.42) is the first raw figure. Most of it is which projects each judge happened to see "
        "and review noise, not the judges' own lean, which the model puts at about 0.16. A method that drives the "
        "spread to zero (centering or z-scoring each judge) erases real differences between the projects each judge "
        "was assigned along with the lean; Part 2 measures what that costs.",
        "",
        "Every ranked project, raw against normalized, fitted as the portal fits it (0-100 scale, all 126 reviews: "
        "the superseded duplicate prj_07 is not ranked, but its 5 reviews still show how its judges lean; \"beats "
        "next\" is the calibrated chance a project truly outranks the one below it):",
        "",
        "| rank | raw rank | move | project | reviews | raw | adjusted | 90% interval | beats next |",
        "|---:|---:|---:|---|---:|---:|---:|---|---:|",
    ]
    f = fit([Observation(s["judge"], s["project"], combine(s["criteria"], crit)) for s in fx["scores"]],
            ranked={p["id"] for p in fx["projects"]} - {DUPLICATE})
    for p in f.projects:
        move = p.raw_rank - p.rank
        lines.append(f"| {p.rank} | {p.raw_rank} | {move:+d} | {titles[p.project]} ({p.project}) | {p.n_reviews} | "
                     f"{p.raw_mean:.1f} | {p.adjusted:.1f} | {p.low:.1f} to {p.high:.1f} | "
                     f"{'' if p.p_above_next is None else f'{p.p_above_next:.0%}'} |")
    moved = sum(1 for p in f.projects if p.rank != p.raw_rank)
    beats = [p.p_above_next for p in f.projects if p.p_above_next is not None]
    sp = f.spread_summary
    lines += ["", f"{moved} of {len(f.projects)} projects change rank, by at most "
                  f"{max(abs(p.rank - p.raw_rank) for p in f.projects)} places. The true spread between projects is "
                  f"estimated at {sp['estimate']:.1f} points (90% range {sp['low90']:.1f} to {sp['high90']:.1f}) "
                  f"against review noise of {f.noise_sd:.1f}"
                  f"{', and zero cannot be ruled out' if sp['zero_plausible'] else ''}, so every \"beats next\" "
                  f"is between {min(beats):.0%} and {max(beats):.0%}: ties and leanings at best. The calibration "
                  "check in Part 2 is what makes those numbers trustworthy.", ""]
    return "\n".join(lines) + "\n"


def fixture() -> dict:
    return json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))


# The earlier of the team's two "Dry Harbour" entries. Plumb ranks the later
# one; the earlier one's reviews stay in the fit to calibrate their judges,
# so the simulation keeps them too and scores every method on the rest.
DUPLICATE = "prj_07"


def design(fx: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
    pairs = [(s["judge"], s["project"]) for s in fx["scores"]]
    track = {p["id"]: p["track"] for p in fx["projects"]}
    return pairs, track


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


SCENARIOS = ("additive", "scale", "flat", "track", "discrete", "no-bias", "fixture")
BINS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001)]
METHODS = ("raw", "zscore", "plumb", "pairwise")
CRITERIA = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("a", "b", "c")]


def simulate(pairs, track_of, rng, scenario: str, q_sd=12.0, b_sd=8.0, e_sd=10.0):
    if scenario == "fixture":
        q_sd, b_sd, e_sd = 4.0, 4.3, 15.9
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
        coverage, stated = [], []
        calib = []  # (stated P(beats next), 1 if the true order agrees)
        for _ in range(draws):
            fitted, truth, obs = simulate(pairs, track_of, rng, scenario)
            projects = [p for p in fitted if p != DUPLICATE]
            t = np.array([truth[p] for p in projects])
            f = fit(obs, ranked=set(projects))
            est = {
                "raw": raw_means(obs),
                "zscore": zscore_means(obs),
                "plumb": {r.project: r.adjusted for r in f.projects},
                "pairwise": {r.project: r.strength for r in pairwise.fit(pairwise.implied(obs), fitted).strengths},
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
            for upper, lower in zip(f.projects, f.projects[1:]):
                if upper.p_above_next is not None:
                    calib.append((upper.p_above_next, float(truth[upper.project] > truth[lower.project])))
            shift = np.mean([r.adjusted for r in f.projects]) - t.mean()
            coverage.append(np.mean([r.low - shift <= truth[r.project] <= r.high - shift for r in f.projects]))
            # As printed: the planted truth is centred on 50 like the scores, so
            # the stated level can be checked too, not only the relative one.
            stated.append(np.mean([r.low <= truth[r.project] <= r.high for r in f.projects]))
        res = {}
        raw_sp = np.array(acc["raw"]["spearman"])
        for m, d in acc.items():
            diff = np.array(d["spearman"]) - raw_sp
            res[m] = {k: float(np.mean(v)) for k, v in d.items()}
            res[m]["vs_raw"] = float(diff.mean())
            res[m]["vs_raw_ci"] = float(1.96 * diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else 0.0
        res["plumb"]["coverage90"] = float(np.mean(coverage))
        res["plumb"]["stated90"] = float(np.mean(stated))
        cal = np.array(calib)
        res["brier"] = float(np.mean((cal[:, 0] - cal[:, 1]) ** 2)) if len(cal) else None
        res["calibration"] = []
        for lo, hi in BINS:
            sel = cal[(cal[:, 0] >= lo) & (cal[:, 0] < hi)] if len(cal) else cal
            if len(sel):
                res["calibration"].append({"bin": f"{lo:.0%}-{min(hi, 1):.0%}", "n": int(len(sel)),
                                           "stated": float(sel[:, 0].mean()), "observed": float(sel[:, 1].mean())})
        out[scenario] = res
    return out


def report(res: dict, draws: int, seed: int) -> str:
    lines = [
        "## Part 2: simulation with known ground truth",
        "",
        f"{draws} draws per scenario, seed {seed}, on the fixture's judge-project graph "
        "(126 reviews, 30 judges, 40 ranked projects; the superseded duplicate is fitted, not scored; "
        "true project sd 12, judge lean sd 8, noise sd 10).",
        "",
        "| scenario | method | rmse | spearman | vs raw (95% MC interval) | true top 5 found | 90% coverage | as stated |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario, methods in res.items():
        for m, r in methods.items():
            if m in ("calibration", "brier"):
                continue
            cov = f"{r['coverage90']:.0%}" if "coverage90" in r else ""
            st = f"{r['stated90']:.0%}" if "stated90" in r else ""
            vs = "" if m == "raw" else f"{r['vs_raw']:+.3f} (±{r['vs_raw_ci']:.3f})"
            lines.append(f"| {scenario} | {m} | {r['rmse']:.2f} | {r['spearman']:.3f} | {vs} | {r['top5']:.2f} | {cov} | {st} |")
    briers = ", ".join(f"{s} {m['brier']:.3f}" for s, m in res.items() if m["brier"] is not None)
    lines += ["", "Calibration of \"beats next\": stated probability against how often the true order agreed. "
              f"Brier score per scenario (0.25 is a coin, lower is better): {briers}.", "",
              "| scenario | bin | pairs | stated | observed |", "|---|---|---:|---:|---:|"]
    for scenario, methods in res.items():
        for c in methods["calibration"]:
            lines.append(f"| {scenario} | {c['bin']} | {c['n']} | {c['stated']:.3f} | {c['observed']:.3f} |")
    return "\n".join(lines) + "\n"


# Part 3: small events. Random balanced designs (each project reviewed by
# `reviews` distinct judges), judge lean sd 4.3 and noise sd 15.9 as in the
# fixture, true spread from fixture-like (4) to clearly separated (16).
SMALL = [(10, 5, 2), (15, 6, 3), (25, 8, 2), (40, 10, 3)]
SPREADS = (4.0, 8.0, 16.0)
SMALL_BINS = [(0.6, 0.75), (0.75, 0.9), (0.9, 1.0001)]


def small_events(draws: int, seed: int, designs=SMALL, spreads=SPREADS) -> list[dict]:
    rng = np.random.default_rng(seed)
    rows = []
    for P, J, r in designs:
        for q_sd in spreads:
            calib, flagged, dof = [], [], 0
            for _ in range(draws):
                truth = rng.normal(50, q_sd, P)
                lean = rng.normal(0, 4.3, J)
                obs = [Observation(f"j{j}", f"p{p}", truth[p] + lean[j] + rng.normal(0, 15.9))
                       for p in range(P) for j in rng.choice(J, r, replace=False)]
                f = fit(obs)
                dof = f.dof
                flagged.append(f.few_reviews)
                for upper, lower in zip(f.projects, f.projects[1:]):
                    # Forced: below MIN_DOF the portal withholds these, and this is the evidence why.
                    p = f.p_better(upper.project, lower.project, force=True)
                    if p is not None:
                        calib.append((p, float(truth[int(upper.project[1:])] > truth[int(lower.project[1:])])))
            cal = np.array(calib)
            bins = []
            for lo, hi in SMALL_BINS:
                sel = cal[(cal[:, 0] >= lo) & (cal[:, 0] < hi)]
                bins.append({"bin": f"{lo:.0%}-{min(hi, 1):.0%}", "n": int(len(sel)),
                             "stated": float(sel[:, 0].mean()) if len(sel) else None,
                             "observed": float(sel[:, 1].mean()) if len(sel) else None})
            rows.append({"design": f"{P} projects, {r} reviews", "dof": dof, "spread": q_sd,
                         "few_reviews": float(np.mean(flagged)), "brier": float(np.mean((cal[:, 0] - cal[:, 1]) ** 2)),
                         "bins": bins})
    return rows


def small_report(rows: list[dict], draws: int, seed: int) -> str:
    lines = ["## Part 3: small events", "",
             f"{draws} draws per row, seed {seed}. Random balanced designs, judge lean sd 4.3, review noise sd 15.9 "
             "(as in the fixture), true spread between projects 4 (fixture-like), 8 or 16. Each cell is stated → "
             f"observed (pairs). Below {MIN_DOF} residual degrees of freedom the portal states no probabilities; "
             "they are computed here anyway to show why.", "",
             "| design | dof | spread | withheld | brier | 60-75% | 75-90% | 90%+ |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        cells = [f"{b['stated']:.2f} → {b['observed']:.2f} ({b['n']})" if b["n"] else "none stated" for b in r["bins"]]
        lines.append(f"| {r['design']} | {r['dof']} | {r['spread']:.0f} | {r['few_reviews']:.0%} | {r['brier']:.3f} | "
                     + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--small-draws", type=int, default=300)
    ap.add_argument("--out", default=None, help="also write the report to this file")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the report has arrows; a Windows console defaults to cp1252
    text = (fixture_section(fixture()) + "\n" + report(evaluate(args.draws, args.seed), args.draws, args.seed) + "\n"
            + small_report(small_events(args.small_draws, args.seed), args.small_draws, args.seed))
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("# Normalization proof: output\n\nGenerated by `python tools/normalization_proof.py "
                                  f"--draws {args.draws} --seed {args.seed} --small-draws {args.small_draws} "
                                  f"--out {args.out}`.\n\n" + text,
                                  encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
