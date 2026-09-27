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

**Per project** Plumb reports the adjusted score `quality[p]`, a 90% interval, the raw mean, how far its judges' lean moved it ("its judges scored 1.2 points high"), and `P(beats next)`: the probability that its true quality exceeds that of the project ranked just below.

**Probabilities are empirical Bayes.**
The adjusted scores are deliberately not shrunk (see section 4).
But a probability that one project truly beats another has to account for how alike projects are: when the true differences are small next to the noise, a gap between two estimates is mostly noise.
So every probability Plumb states ("beats next", track leads, prize confidence) comes from a posterior that puts a Normal(mean, spread^2) prior on true quality, with the spread estimated from the data (the variance of the adjusted scores minus their average estimation variance), combined with the estimates' full joint covariance (so shared judges are accounted for, not treated as independent): `P(a beats b) = Phi(diff_post / sd(diff_post))`.
Section 5 shows why this is necessary: without it, a stated 84% came true only 60% of the time on fixture-like data.
Every probability is shown with one vocabulary: under 60% is a *tie*, 60 to 75% *leaning*, 75 to 90% *likely*, 90% and up *clear*.

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

**What the flat judge becomes:** `jdg_07` is not dropped. Their three identical scores still say "these are all about a 75", which, compared with other judges on the same projects, estimates their lean (+1.7 points). Their reviews carry no information about the order of their own three projects, and the organizer dashboard flags them for a conversation.

## 5. The normalization proof

We cannot know the true quality of the fixture projects, so `tools/normalization_proof.py` plants one.
It keeps the fixture's real judge-project graph (122 reviews after leaving out the duplicate's 4, 40 projects, 30 judges, unbalanced exactly as delivered), invents true qualities (sd 12) and judge leans (sd 8), draws scores with noise (sd 10), and asks each method to recover the planted truth.

Seven scenarios test the assumptions:

* `additive`: judges differ by a constant lean (the model's own assumption).
* `scale`: judges also stretch or squash their scores, which the model does not model.
* `flat`: `jdg_07` gives everyone the same score, as in the fixtures.
* `track`: tracks differ in true quality and judges stay in their tracks, as the fixture judges do. This is the argument against z-scores, tested.
* `discrete`: every review is three whole-number 1 to 5 criteria, with the floor and ceiling that implies, combined the way Plumb combines a rubric.
* `no-bias`: every judge is fair. Does the correction cost anything?
* `fixture`: calibrated to what the model finds in the real fixture reviews: judge lean sd 4.3, noise sd 15.9, and a small true spread between projects (sd 4). This is the regime Plumb actually faces on this data.

Four methods: `raw` means, per-judge `zscore`s, `plumb`'s model, and `pairwise`: Bradley-Terry on the preferences implied within each judge's scores (section 8), which ignores every judge's scale.

The full output is in [`docs/normalization-proof.md`](docs/normalization-proof.md): part 1 is the method run on the real fixtures (before and after), part 2 this simulation, 1000 draws per scenario.
The rank correlation with the truth (Spearman), and the paired difference from raw means with its 95% Monte Carlo interval:

| scenario | raw | zscore | plumb | pairwise | plumb vs raw | plumb 90% coverage |
|---|---:|---:|---:|---:|---:|---:|
| additive | 0.823 | 0.772 | **0.841** | 0.763 | +0.018 (±0.002) | 90% |
| scale | 0.834 | 0.770 | **0.849** | 0.761 | +0.015 (±0.002) | 90% |
| flat | 0.808 | 0.771 | **0.828** | 0.762 | +0.021 (±0.002) | 90% |
| track | 0.862 | 0.704 | **0.877** | 0.757 | +0.014 (±0.002) | 90% |
| discrete | 0.839 | 0.796 | **0.861** | 0.805 | +0.022 (±0.002) | 91% |
| no-bias | 0.875 | 0.775 | 0.875 | 0.765 | -0.000 (±0.000) | 90% |
| fixture | 0.359 | 0.307 | 0.359 | 0.274 | -0.000 (±0.001) | 90% |

What this shows:

* **Plumb ranks best in every scenario where judges differ by a meaningful amount**, including the two it does not model (`scale`, `discrete`), and every gain is many times its Monte Carlo error. It ties raw means when judges are fair (`no-bias`) or barely differ (`fixture`), so the correction costs nothing when it is not needed.
* **Z-scores fall apart when tracks differ** (0.704 against 0.862 for doing nothing at all), which is the situation track-based assignment creates. They are worse than raw means in every scenario.
* **The intervals are honest.** Plumb's 90% intervals contain the planted truth 90 to 91% of the time in all seven scenarios. The "beats next" column, the award probabilities and the certificates all rest on this.
* **The gains are modest.** With about three reviews per project, no method can do much better. The honest thing is to say how uncertain the ranking is, which is what the intervals are for.
* **Scale-free is not better here.** Implied pairwise preferences throw away the size of every score difference, and on a graph this sparse that costs more than removing each judge's scale gains. It is kept as a cross-check, not as the ranking.

On rmse (error on the score scale) the picture is the same except under `scale`, where z-scores edge out Plumb (7.18 against 7.33) because rmse rewards matching each judge's stretching; the rank correlation, which is what prizes need, favours Plumb there too.

**The fixture regime is humbling.**
With the real data's noise, no method recovers the true order well (rank correlation 0.36 for the best of them), and judge lean is too small there for correcting it to change much.
That is not a failure of the method; it is what 3 reviews per project with this much disagreement can support.
The job of the engine in that regime is to say so, which is what the next check is about.

**Calibration of the probabilities.**
Across every draw, every adjacent pair in Plumb's ranking gets a "beats next" probability.
Binned, the share of pairs whose true order matched should equal the stated probability.
The first version of Plumb computed these from the unshrunk estimates, and the check caught it overstating confidence badly in the fixture regime (stated 84%, observed 60%).
With the empirical-Bayes posterior (section 3) the same check reads:

| scenario | stated 50-60% | stated 60-70% | stated 70-80% | stated 80-90% | stated 90%+ |
|---|---:|---:|---:|---:|---:|
| additive | 0.547 → 0.544 | 0.641 → 0.648 | 0.740 → 0.741 | 0.840 → 0.821 | 0.943 → 0.958 |
| discrete | 0.545 → 0.549 | 0.642 → 0.640 | 0.741 → 0.739 | 0.841 → 0.822 | 0.940 → 0.946 |
| no-bias | 0.539 → 0.538 | 0.637 → 0.629 | 0.742 → 0.759 | 0.843 → 0.855 | 0.946 → 0.929 |
| fixture | 0.519 → 0.521 | 0.632 → 0.598 | 0.733 → 0.675 | 0.833 → 0.750 (16 pairs) | none stated |
| track | 0.548 → 0.546 | 0.643 → 0.577 | 0.742 → 0.658 | 0.840 → 0.761 | 0.947 → 0.939 |

(Stated → observed. Full table, including `scale` and `flat`, in [`docs/normalization-proof.md`](docs/normalization-proof.md).)
Close to calibrated where the model's assumptions hold, and still somewhat optimistic in two cases: when tracks differ in quality (the prior treats all projects as alike, so it under-shrinks pairs from different tracks) and in the 70% and higher bins of the fixture regime.
Those are stated here rather than hidden; a per-track prior is the natural next step.

`tests/test_proof.py` re-runs a small seeded version on every test run and fails if Plumb stops beating raw means, raw means stop beating z-scores, or the intervals stop covering 86 to 94%.

## 6. What the fixture data says

On a fresh boot, the seeded fixture event (121 reviews: the 126 in the file minus the 5 of the superseded duplicate) reports: typical judge lean plus or minus 3.9 points, review-to-review noise plus or minus 15.1 points (on 0 to 100), and no detectable true spread between projects once estimation noise is removed.
In plain words: in the fixture reviews, two judges looking at the same project disagree far more than projects differ from each other.
Correcting for judges moves 25 of 40 projects relative to raw means (at most 5 places), by between -0.8 and +1.2 points each, and 90% intervals are 11.5 to 18 points wide on each side.
Because no true spread is detectable, every calibrated probability is a coin flip: each "beats next", each track lead and each prize reads 50%, a *tie*.

**The organizers' σ = 0.42.**
The DOGFOOD homepage quotes the judge spread in the fixtures: the sample standard deviation of each judge's mean score (the mean of a review's three 1 to 5 criteria), over all 126 reviews, which is 0.42.
Plumb reproduces it, and reports what happens to it: removing each judge's estimated lean takes it to 0.37.
It does not go to zero, and should not: judges saw 1 to 11 projects each, from different tracks, with review noise of about 0.6 on this scale, so most of the 0.42 is which projects a judge happened to see plus noise.
The model puts the true spread of judge lean at about 0.16.
A method that drives the spread to zero (centering or z-scoring every judge) does so by construction, erasing real differences between the projects each judge was assigned; the `track` scenario above measures that cost.
With the superseded duplicate left out (as Plumb ranks it), the same figures are 0.33 and 0.28.
The full before/after, every project's raw score, adjusted score, interval and rank change, opens [`docs/normalization-proof.md`](docs/normalization-proof.md).

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
| Duplicate submission (`prj_07`, `prj_41`: same team, title and repo, 13.5 hours apart; the later one three minutes before the deadline) | Imported with the team's latest submission as its entry (its final word before the deadline) and the earlier one marked as the duplicate, excluded from gallery, ballot and ranking. The organizer dashboard and the publish checklist ask a person to confirm or swap it. Reviews are never moved between entries: a judge scored what they saw |
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

The organizer's results page shows the Bradley-Terry ranking from direct comparisons, and the same estimator on the preferences implied by rubric scores, each with its Spearman agreement with the model's ranking (0.87 for the implied preferences on the fixture data), and flags any project whose pairwise and score ranks differ by 10 or more places.
A judge who finds a pair too close to call can skip it.
Scores remain the ranking of record; pairwise is a second opinion that answers "would the judges' head-to-head picks agree?".

## 9. Winners

At publication the organizer confirms a winner for every prize.
Plumb suggests one: a track prize goes to the track leader, overall prizes go down the overall ranking in prize order.
Next to each suggestion it shows how sure the model is that the winner beats the closest rival (the runner-up, or for later overall prizes the best project that has not already won one), in the tie / leaning / likely / clear vocabulary.
The organizer can pick someone else; the signed record then says the organizers chose a project other than the top-ranked candidate, so an override is visible, not hidden.
No project can win two overall prizes, and each later overall suggestion follows the choices already made.
On the fixture data every prize is a *tie*, and the results page says exactly that next to each winner.

**The rubric.**
Organizers can lock the rubric (typically as judging opens) so nobody can reweight after seeing the live ranking.
Whether it was locked, and how many times it was reweighted after the first review, is part of the signed results.

## 10. Community votes are separate

The community vote is its own tally with its own rules: per-voter shuffled ballots, a fixed number of votes each, and secrecy that holds against organizers too.
While voting is open, counts are hidden from everyone, and each vote enters the audit log only as a seal, `sha256(nonce:project)`, so the log (and any webhook streaming it) proves a vote was cast without saying for what.
An open vote can be extended but never shortened or switched off, so nobody can end it at a convenient moment.
After it closes, the nonces are revealed: anyone can recompute every seal, find it in the audit log, and check the tally against history.
The vote is never mixed into the judged ranking, and judged results cannot be published until it has closed.

## 11. Publication and verification

Publishing freezes the ranking into a signed record (Ed25519) that includes the awards, the rubric weights and their history, the fitted judge lean, noise and spread, every project's interval and "beats next", and the head hash of the audit log at that moment.
The issuer in every record is the configured public address (`PLUMB_BASE_URL`), never a request's Host header; publishing is refused until it is set.
Each judge gets a signed participation record with a SHA-256 digest of their own reviews, which they can recompute from their export; each team gets a signed certificate with its rank, interval and any prize.
After publication the schedule, name, tracks, prizes, reviews, rubric, duplicates and disqualifications are frozen, and the public results page is rendered from the signed record itself, so what the public reads is exactly what was signed.
`tools/verify_record.py` checks any record offline against a pinned public key.

## 12. Limitations

* The model is additive. Judges who stretch or squash their scores are not modelled; with three reviews per judge on average a per-judge scale is not estimable, and the proof shows the additive model still ranks best when judges do this.
* Gaussian noise is assumed. Scores are bounded and discrete; the `discrete` scenario shows the intervals stay calibrated anyway.
* Lean is assumed constant across criteria and across the judging window.
* Probabilities use one prior for all projects; when tracks differ in quality they are somewhat optimistic for pairs from different tracks (section 5).
* The model cannot tell a lenient judge from a judge who drew better projects unless those projects are also seen by other judges. Coverage and bridging assignments make the difference identifiable, which is why the assigner prefers them.
