"""The probability model: Student's t, the spread and the evidence flags.
Each test pins a property with a known answer."""

import numpy as np
import pytest

from app.normalize import MIN_DOF, Observation, fit, t_cdf, t_quantile


def design(rng, quality: dict[str, float], judges: int, reviews: int, noise: float, lean: float = 0.0):
    """Each project reviewed by `reviews` distinct judges, spread evenly."""
    leans = {f"j{i}": rng.normal(0, lean) if lean else 0.0 for i in range(judges)}
    names = list(leans)
    obs = []
    for k, (p, q) in enumerate(quality.items()):
        for r in range(reviews):
            j = names[(k * reviews + r) % judges]
            obs.append(Observation(j, p, q + leans[j] + rng.normal(0, noise)))
    return obs


@pytest.mark.parametrize("p, dof, expected", [
    (0.95, 1, 6.313752), (0.95, 2, 2.919986), (0.95, 10, 1.812461), (0.975, 10, 2.228139),
    (0.95, 30, 1.697261), (0.95, 1000, 1.646379), (0.5, 7, 0.0), (0.05, 10, -1.812461),
])
def test_t_quantiles_match_the_tables(p, dof, expected):
    assert t_quantile(p, dof) == pytest.approx(expected, abs=1e-5)
    assert t_cdf(expected, dof) == pytest.approx(p, abs=1e-6)


def test_intervals_use_t_with_the_residual_dof():
    rng = np.random.default_rng(3)
    obs = design(rng, {f"p{i}": 50 + 3 * i for i in range(8)}, judges=4, reviews=3, noise=5)
    f = fit(obs)
    assert f.dof == len(obs) - 8
    p = f.projects[0]
    assert (p.high - p.low) / 2 == pytest.approx(t_quantile(0.95, f.dof) * p.sd)


def test_p_better_is_complementary_and_follows_the_evidence():
    rng = np.random.default_rng(4)
    quality = {"far": 90.0, **{f"p{i}": 50 + rng.normal(0, 4) for i in range(12)}}
    f = fit(design(rng, quality, judges=6, reviews=4, noise=4))
    for a, b in [("far", "p0"), ("p1", "p2"), ("p3", "far")]:
        assert f.p_better(a, b) + f.p_better(b, a) == pytest.approx(1.0, abs=1e-9)
    assert f.p_better("far", "p0") > 0.999
    assert f.projects[0].project == "far"


def test_equal_projects_are_flagged_and_their_probabilities_stay_near_even():
    rng = np.random.default_rng(5)
    f = fit(design(rng, {f"p{i}": 60.0 for i in range(20)}, judges=8, reviews=4, noise=10))
    assert f.barely_separated
    assert f.spread_summary["zero_plausible"]
    beats = [p.p_above_next for p in f.projects if p.p_above_next is not None]
    assert max(beats) < 0.75


def test_well_separated_projects_are_not_flagged():
    rng = np.random.default_rng(6)
    f = fit(design(rng, {f"p{i}": 20 + 4 * i for i in range(20)}, judges=8, reviews=4, noise=4))
    assert not f.barely_separated
    assert not f.few_reviews
    lo, hi = f.spread_summary["low90"], f.spread_summary["high90"]
    assert lo < f.spread_summary["estimate"] < hi


def test_one_review_per_project_says_nothing_about_uncertainty():
    obs = [Observation(f"j{i % 3}", f"p{i}", 40.0 + i) for i in range(6)]
    f = fit(obs)
    assert not f.identifiable
    assert f.judge_sd is None and f.noise_sd is None and f.spread is None
    for p in f.projects:
        assert p.sd is None and p.low is None and p.high is None and p.p_above_next is None
    assert f.p_better("p0", "p1") is None
    assert not f.few_reviews   # not "few": there is nothing left over at all


def test_small_events_state_no_probabilities_and_larger_ones_do():
    from app.domain import award_check
    rng = np.random.default_rng(7)
    small = fit(design(rng, {f"p{i}": 50 + 2 * i for i in range(10)}, judges=5, reviews=2, noise=5))
    assert small.dof == 10 < MIN_DOF and small.few_reviews
    assert all(p.p_above_next is None for p in small.projects)
    a, b = small.projects[0].project, small.projects[1].project
    assert small.p_better(a, b) is None and 0 < small.p_better(a, b, force=True) < 1
    assert all(p.low is not None for p in small.projects)          # the intervals stay
    assert award_check(small, a, [a, b])["close_call"]             # no number to lean on: a reason is needed
    larger = fit(design(rng, {f"p{i}": 50 + 2 * i for i in range(15)}, judges=6, reviews=3, noise=5))
    assert larger.dof == 30 and not larger.few_reviews
    assert all(p.p_above_next is not None for p in larger.projects[:-1])


def test_tied_projects_are_ordered_by_id_on_every_run():
    obs = []
    for p in ("pb", "pa", "pc"):
        obs += [Observation("j1", p, 70.0), Observation("j2", p, 72.0)]
    obs += [Observation("j1", "pd", 40.0), Observation("j2", "pd", 45.0)]
    orders = {tuple(r.project for r in fit(list(reversed(obs)) if k % 2 else obs).projects) for k in range(4)}
    assert orders == {("pa", "pb", "pc", "pd")}


def test_probabilities_are_calibrated_where_the_evidence_is_enough():
    """Draw events with a known truth and check that stated probabilities
    come true about as often as stated. 30 events of 15 projects with 3
    reviews each, true spread twice the review noise: the regime where the
    simulations found the model honest."""
    rng = np.random.default_rng(8)
    stated, happened = [], []
    for _ in range(30):
        quality = {f"p{i}": rng.normal(50, 10) for i in range(15)}
        f = fit(design(rng, quality, judges=6, reviews=3, noise=5, lean=4))
        for upper, lower in zip(f.projects, f.projects[1:]):
            stated.append(upper.p_above_next)
            happened.append(quality[upper.project] > quality[lower.project])
    stated, happened = np.array(stated), np.array(happened, dtype=float)
    assert abs(stated.mean() - happened.mean()) < 0.06
    confident = stated > 0.8
    assert confident.sum() >= 30
    assert happened[confident].mean() > 0.8
    assert np.mean((stated - happened) ** 2) < 0.25   # better than a coin



@pytest.mark.parametrize("p, words", [
    (0.97, "97% &middot; clear"), (0.8, "80% &middot; likely"), (0.65, "65% &middot; leaning"),
    (0.5, "50% &middot; tie with Rival"), (0.2, "20% &middot; Rival likely ahead"),
])
def test_certainty_words(p, words):
    from app.web import templates
    macro = templates.env.from_string('{% from "_macros.html" import certainty %}{{ certainty(p, "Rival") }}')
    assert words in macro.render(p=p)


def test_the_method_note_marks_small_events_as_rough():
    from types import SimpleNamespace

    from app.web import templates
    rng = np.random.default_rng(7)
    small = fit(design(rng, {f"p{i}": 50 + 2 * i for i in range(10)}, judges=5, reviews=2, noise=5))
    html = templates.env.get_template("_method.html").render(fit=small, set_aside=0, event=SimpleNamespace(
        results_published_at="x"))
    assert "Few reviews: only 10 beyond one per project, so Plumb states no probabilities" in html
    larger = fit(design(rng, {f"p{i}": 50 + 2 * i for i in range(15)}, judges=6, reviews=3, noise=5))
    assert "Few reviews" not in templates.env.get_template("_method.html").render(fit=larger, set_aside=0)


def test_a_shown_60_percent_is_never_called_a_tie():
    from app.domain import award_check
    from app.web import templates
    macro = templates.env.from_string('{% from "_macros.html" import certainty %}{{ certainty(p) }}')
    assert "60% &middot; leaning" in macro.render(p=0.598)
    assert "59% &middot; tie" in macro.render(p=0.594)
    f = type("F", (), {"p_better": lambda self, a, b: 0.598})()
    assert not award_check(f, "a", ["a", "b"])["close_call"]
    f = type("F", (), {"p_better": lambda self, a, b: 0.594})()
    assert award_check(f, "a", ["a", "b"])["close_call"]


@pytest.mark.parametrize("p", [0.5949, 0.595, 0.5951, 0.5999, 0.6, 0.745, 0.755, 0.895, 0.9049])
def test_band_and_figure_agree_at_every_boundary(p):
    from app.domain import award_check, shown_percent
    from app.web import templates
    html = templates.env.from_string('{% from "_macros.html" import certainty %}{{ certainty(p) }}').render(p=p)
    shown = shown_percent(p)
    assert f"{shown}%" in html
    word = "tie" if shown < 60 else "leaning" if shown < 75 else "likely" if shown < 90 else "clear"
    assert word in html
    f = type("F", (), {"p_better": lambda self, a, b: p})()
    assert award_check(f, "a", ["a", "b"])["close_call"] == (word == "tie")
