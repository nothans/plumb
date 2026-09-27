# Judging in Plumb

This document covers how judges get their projects, how scores become a ranking, how sure that ranking is, and how winners are chosen and recorded.
Everything here runs in `app/assign.py` (assignment), `app/normalize.py` (the scoring model), `app/pairwise.py` (Bradley-Terry) and `app/records.py` (publication and signed records).
Every number quoted is reproducible: `python tools/normalization_proof.py` for the simulation, and the seeded fixture event on a fresh `docker compose up` for the rest.

## 1. Assignment

The organizer sets a target number of reviews per project (default 3) and presses one button.
`assign.propose` fills the gap without touching anything that already exists:

1. **Track fit.** A judge only gets projects from tracks they are listed for. A judge with no tracks can take anything.
2. **Conflict of interest.** Never a judge who is on the team, and never one who shares a work email domain with a team member. Shared public mail providers (gmail.com and the like, plus the RFC 2606 documentation domains the fixtures use) do not count. Judges and organizers cannot be on a team in the same event at all.
3. **Coverage first.** The least-reviewed project is filled first, so coverage evens out before anyone gets a fourth review.
4. **Load balance.** Among eligible judges, the one with the lightest load wins.
5. **Bridging.** On a tie, prefer a judge not yet connected (through shared projects) to the project's other judges. Leniency can only be compared through shared projects, so a judge graph in one connected piece is what makes normalization possible at all.
6. **Deterministic tie-break.** A hash of `(judge, project)` settles the rest, so the same inputs give the same assignment and no judge is favoured by id order.

Projects that cannot reach the target are listed for the organizer instead of being silently short.
Manual assignment applies the same conflict check.
An assignment cannot be removed once the judge has reviewed it: the review stays on the record.

The fixture judges only ever scored projects in their own tracks, which is why the importer keeps track assignments and the auto-assigner respects them.

## 2. From criterion scores to one number

Each judge scores each criterion on its own scale (the fixtures use 1 to 5 for functionality, quality and innovation).
A review's score is the weighted mean of its criteria after mapping each onto 0..1 with its own minimum and maximum, reported on 0 to 100:

```
score = 100 * sum_c( w_c * (v_c - min_c) / (max_c - min_c) ) / sum_c( w_c )
```

Mapping first means a 1-10 criterion and a 1-5 criterion weigh what their weights say, not what their ranges say.
Weights are applied at read time, so the organizer can reweight any time before publication and the live ranking follows; every change is audited, and the weights in force are frozen into the signed results.
A review missing a weighted criterion is excluded from the ranking and counted on the results page, never guessed.

## 3. The model

Judges differ.
Some are harsh, some generous; each reviews a handful of projects from their own tracks.
Plumb models every review as

```
score[j, p] = quality[p] + leniency[j] + noise

quality[p]   fixed effect, one per project, no prior
leniency[j]  ~ Normal(0, var_judge)
noise        ~ Normal(0, var_noise)
```

This is a linear mixed model with projects as fixed effects and judges as random effects.

**Estimation.**
`var_judge` and `var_noise` are estimated by restricted maximum likelihood (REML).
With `lambda = var_noise / var_judge`, the -2 REML log-likelihood, up to a constant, is

```
(n - P) * log(var_noise_hat) - J * log(lambda) + log det(X'X + lambda * D_J)
```

where `n` is the number of reviews, `P` projects, `J` judges, `X` the review-by-(project, judge) indicator matrix, `D_J` the identity on the judge block, and `var_noise_hat = y'(y - X m) / (n - P)` the profiled noise variance.
It is minimised over `log(lambda)` in [-12, 12] by a 25-point grid followed by golden-section search.
Given `lambda`, Henderson's mixed-model equations `(X'X + lambda * D_J) m = X'y` give the estimates `m`, and `var_noise * (X'X + lambda * D_J)^-1` their joint covariance.
A fit on the fixture data takes about 6 ms.

**Per project** Plumb reports the adjusted score `quality[p]`, a 90% interval, the raw mean, how far its judges' lean moved it ("its judges ran 3.1 harsh"), and `P(beats next)`: the probability that its true quality exceeds that of the project ranked just below, `Phi(diff / sd(diff))`, from the joint covariance (shared judges are accounted for, not treated as independent).

**Per judge** Plumb reports the estimated lean with its standard error, and flags: `flat` (every review identical), `few-reviews` (fewer than 3, so the estimate leans on the prior), and `harsh` or `generous` when the lean is more than 1.645 posterior standard deviations from zero (a Bayesian reading of the estimate, not a frequentist test that the lean is exactly zero).

**When it cannot say.**
If there are no more reviews than projects, nothing is left over to estimate noise from.
Plumb then reports no intervals and no "beats next" rather than false certainty, and refuses to publish until more reviews come in.

**Connectivity.**
Plumb counts the connected pieces of the judge-project graph.
If there is more than one, leniency cannot be compared across them; the results page says so and asks for bridging assignments.
The fixture graph is one piece.

## 4. Why this model and not the usual ones

**Why not per-judge z-scores** (the textbook method, and the most common answer in hackathon tooling):

* A z-score divides by the judge's own standard deviation. `jdg_07` in the fixtures gave all three of their projects the same scores (4/4/4), so their standard deviation is zero; `jdg_01` and `jdg_23` filed one review each, so they have none. Implementations quietly map these to zero, which throws the reviews away.
* A z-score assumes every judge saw a random slice of the field. Judges are assigned by track. A judge who drew a strong track looks harsh to a z-score when they were simply right, and their projects are punished for it. Section 5 measures exactly this.
* With one to eleven reviews per judge (median 3), a per-judge mean and spread are mostly noise, and the z-score treats that noise as signal.

**Why judges are random effects:** a judge with two reviews cannot support a precise leniency estimate. The prior shrinks them toward "typical" and lets an eleven-review judge speak for themselves.

**Why projects are fixed effects (not shrunk):** project quality is what the prizes are decided on. Shrinking it would pull a project with two reviews toward the middle harder than one with five, marking a team down for how many judges happened to see it. With projects fixed, fewer reviews show up as a wider interval instead of a lower score.

**What the flat judge becomes:** `jdg_07` is not dropped. Their three identical scores still say "these are all about a 75", which, compared with other judges on the same projects, estimates their lean (+1.8 points). Their reviews carry no information about the order of their own three projects, and the organizer dashboard flags them for a conversation.

## 5. The normalization proof

We cannot know the true quality of the fixture projects, so `tools/normalization_proof.py` plants one.
It keeps the fixture's real judge-project graph (122 reviews after leaving out the duplicate's 4, 40 projects, 30 judges, unbalanced exactly as delivered), invents true qualities (sd 12) and judge leans (sd 8), draws scores with noise (sd 10), and asks each method to recover the planted truth.

Six scenarios test the assumptions:

* `additive`: judges differ by a constant lean (the model's own assumption).
* `scale`: judges also stretch or squash their scores, which the model does not model.
* `flat`: `jdg_07` gives everyone the same score, as in the fixtures.
* `track`: tracks differ in true quality and judges stay in their tracks, as the fixture judges do. This is the argument against z-scores, tested.
* `discrete`: every review is three whole-number 1 to 5 criteria, with the floor and ceiling that implies, combined the way Plumb combines a rubric.
* `no-bias`: every judge is fair. Does the correction cost anything?

Four methods: `raw` means, per-judge `zscore`s, `plumb`'s model, and `pairwise`: Bradley-Terry on the preferences implied within each judge's scores (section 8), which ignores every judge's scale.

The full output, 1000 draws per scenario, is in [`docs/normalization-proof.md`](docs/normalization-proof.md).
The rank correlation with the truth (Spearman), and the paired difference from raw means with its 95% Monte Carlo interval:

| scenario | raw | zscore | plumb | pairwise | plumb vs raw | plumb 90% coverage |
|---|---:|---:|---:|---:|---:|---:|
| additive | 0.827 | 0.768 | **0.845** | 0.763 | +0.018 (±0.002) | 91% |
| scale | 0.836 | 0.769 | **0.849** | 0.761 | +0.014 (±0.002) | 90% |
| flat | 0.807 | 0.766 | **0.830** | 0.760 | +0.023 (±0.002) | 90% |
| track | 0.865 | 0.704 | **0.878** | 0.758 | +0.013 (±0.001) | 91% |
| discrete | 0.841 | 0.794 | **0.862** | 0.804 | +0.021 (±0.002) | 91% |
| no-bias | 0.873 | 0.769 | 0.873 | 0.764 | -0.000 (±0.000) | 90% |

What this shows:

* **Plumb ranks best in every scenario with biased judges**, including the two it does not model (`scale`, `discrete`), and every gain is many times its Monte Carlo error. It ties raw means exactly when judges are fair, so the correction costs nothing when it is not needed.
* **Z-scores fall apart when tracks differ** (0.704 against 0.865 for doing nothing at all), which is the situation track-based assignment creates. They are worse than raw means in every scenario.
* **The intervals are honest.** Plumb's 90% intervals contain the planted truth 90 to 91% of the time in all six scenarios. The "beats next" column, the award probabilities and the certificates all rest on this.
* **The gains are modest.** With about three reviews per project, no method can do much better. The honest thing is to say how uncertain the ranking is, which is what the intervals are for.
* **Scale-free is not better here.** Implied pairwise preferences throw away the size of every score difference, and on a graph this sparse that costs more than removing each judge's scale gains. It is kept as a cross-check, not as the ranking.

On rmse (error on the score scale) the picture is the same except under `scale`, where z-scores edge out Plumb (7.22 against 7.28) because rmse rewards matching each judge's stretching; the rank correlation, which is what prizes need, favours Plumb there too.

`tests/test_proof.py` re-runs a small seeded version on every test run and fails if Plumb stops beating raw means, raw means stop beating z-scores, or the intervals stop covering 86 to 94%.

## 6. What the fixture data says

On a fresh boot, the seeded fixture event reports: typical judge lean plus or minus 4.3 points, review-to-review noise plus or minus 15.9 points (on 0 to 100), and an estimated true spread between projects of about zero once estimation noise is removed.
In plain words: in the fixture reviews, two judges looking at the same project disagree far more than projects differ from each other.
The adjusted ranking moves 26 of 40 projects relative to raw means (at most 6 places), every adjacent pair has `P(beats next)` between 50% and 64%, and 90% intervals are 12 to 19 points wide on each side.
Within tracks, the model is between 50% and 80% sure that each track's leader is truly ahead of its runner-up.

A portal that printed a confident 1-to-40 ranking from this data would be telling organizers something the data does not support.
Plumb prints the ranking, shows the intervals, warns on the results page when projects are this hard to separate, and shows the award probabilities at publication, so the organizer knows to treat neighbouring ranks as ties, ask for more reviews, or share a prize.
That warning is the most important output of the judging engine on this dataset.

## 7. Edge cases, and what happens

| Case (fixture example) | What Plumb does |
|---|---|
| Judge gave every project the same score (`jdg_07`) | Kept; lean estimated from shared projects; flagged `flat` on the dashboard |
| Judge with one review (`jdg_01`, `jdg_23`) | Kept; lean shrunk toward typical; flagged `few-reviews` |
| Project with 2 reviews next to one with 5 | Same model, wider interval for the 2-review project, no shrinkage of its score |
| Unfinished review batches | The dashboard leads with the projects below target; auto-assign tops them up |
| Duplicate submission (`prj_07`, `prj_41`: same team, title and repo, 13 minutes apart) | Imported, the later one marked as the duplicate, excluded from gallery, ballot and ranking, shown on the Integrity page, where the organizer can keep the other one instead. Reviews are never moved between entries: a judge scored what they saw |
| Teams sharing a name (three "StillTrail", two "AmberSwitch", two "OpenSignal") | Allowed on import (they are different teams) and reported; new teams in Plumb cannot take a used name |
| Review missing a weighted criterion | Excluded from the ranking and counted on the results page |
| Project with no reviews | Not ranked; listed as unreviewed |
| Only one review per project | No intervals, no "beats next", publication refused (section 3) |
| Judge graph split into pieces | Warned on the results page; auto-assign prefers bridging judges |
| Everyone gave identical scores | No division by zero; every project ties at 50% |

## 8. Pairwise mode (Bradley-Terry)

An organizer can turn on pairwise judging for an event.
Judges then also see two of their assigned projects side by side and pick the better one.
Plumb chooses the pair whose order is least certain for that judge (from their own earlier comparisons only, so the order in which pairs appear cannot hint at other judges' views), never repeats a pair, and shows progress against all possible pairs.

Comparisons are fitted with a Bradley-Terry model, `P(i beats j) = 1 / (1 + exp(-(s_i - s_j)))`, by penalized maximum likelihood (Newton's method) with a Normal(0, 2^2) prior on each strength.
The prior pins the free overall level and keeps a project that won every comparison at a finite strength; standard errors come from the inverse of the penalized observed information.

The organizer's results page shows the Bradley-Terry ranking from direct comparisons, and the same estimator on the preferences implied by rubric scores, each with its Spearman agreement with the model's ranking (0.80 for the implied preferences on the fixture data).
Scores remain the ranking of record; pairwise is a second opinion that answers "would the judges' head-to-head picks agree?".

## 9. Winners

At publication the organizer confirms a winner for every prize.
Plumb suggests one: a track prize goes to the track leader, overall prizes go down the overall ranking in prize order.
Next to each suggestion it shows how sure the model is that the winner beats the closest rival (the runner-up, or for later overall prizes the best project that has not already won one), and warns below 75%.
The organizer can pick someone else; the signed record then says the organizers chose a project other than the top-ranked candidate, so an override is visible, not hidden.

## 10. Community votes are separate

The community vote is its own tally with its own rules (per-voter shuffled ballots, a fixed number of votes each, counts hidden from everyone, organizers included, until voting closes).
It is never mixed into the judged ranking, and judged results cannot be published while the vote is open.

## 11. Publication and verification

Publishing freezes the ranking into a signed record (Ed25519) that includes the awards, the rubric weights, the fitted judge lean, noise and spread, every project's interval and "beats next", and the head hash of the audit log at that moment.
Each judge gets a signed participation record with a SHA-256 digest of their own reviews, which they can recompute from their export; each team gets a signed certificate with its rank, interval and any prize.
After publication the schedule, reviews, rubric, duplicates and disqualifications are frozen, and the public results page is rendered from the signed record itself, so what the public reads is exactly what was signed.
`tools/verify_record.py` checks any record offline against a pinned public key.

## 12. Limitations

* The model is additive. Judges who stretch or squash their scores are not modelled; with three reviews per judge on average a per-judge scale is not estimable, and the proof shows the additive model still ranks best when judges do this.
* Gaussian noise is assumed. Scores are bounded and discrete; the `discrete` scenario shows the intervals stay calibrated anyway.
* Lean is assumed constant across criteria and across the judging window.
* The model cannot tell a lenient judge from a judge who drew better projects unless those projects are also seen by other judges. Coverage and bridging assignments make the difference identifiable, which is why the assigner prefers them.
