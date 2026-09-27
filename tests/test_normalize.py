import math

import numpy as np
import pytest

from app.normalize import Observation, combine, fit, zscore_means


def test_recovers_planted_leniency_on_a_dense_design():
    rng = np.random.default_rng(1)
    quality = {f"p{i}": q for i, q in enumerate(rng.normal(50, 15, 30))}
    lean = {f"j{i}": b for i, b in enumerate([-12, -6, 0, 6, 12, 0])}
    obs = []
    for p, q in quality.items():
        for j, b in lean.items():
            obs.append(Observation(j, p, q + b + rng.normal(0, 2)))
    f = fit(obs)
    est = {j.judge: j.leniency for j in f.judges}
    for j, b in lean.items():
        assert est[j] == pytest.approx(b, abs=1.5)
    adjusted = np.array([f.project(p).adjusted for p in quality])
    truth = np.array(list(quality.values()))
    assert np.corrcoef(adjusted, truth)[0, 1] > 0.99


def test_harsh_judge_does_not_sink_their_projects():
    # Two tracks. Judge "harsh" scores track A 20 points low; "fair" judges
    # overlap with "harsh" on one bridging project, which is enough to learn
    # the lean and undo it.
    obs = []
    true = {"a1": 70, "a2": 60, "b1": 70, "b2": 60, "bridge": 65}
    for p in ("a1", "a2", "bridge"):
        obs.append(Observation("harsh", p, true[p] - 20))
        obs.append(Observation("harsh2", p, true[p] - 20))
    for p in ("b1", "b2", "bridge"):
        obs.append(Observation("fair", p, true[p]))
        obs.append(Observation("fair2", p, true[p]))
    f = fit(obs)
    assert f.project("a1").adjusted == pytest.approx(f.project("b1").adjusted, abs=1.0)
    assert f.project("a2").adjusted == pytest.approx(f.project("b2").adjusted, abs=1.0)
    assert {j.judge for j in f.judges if j.leniency < -5} == {"harsh", "harsh2"}


def test_flat_judge_and_single_review_judge_do_not_break_it():
    obs = [Observation("flat", p, 80) for p in ("p1", "p2", "p3")]
    obs += [Observation("j2", "p1", 90), Observation("j2", "p2", 60), Observation("j2", "p3", 30)]
    obs += [Observation("once", "p2", 55)]
    f = fit(obs)
    assert all(math.isfinite(p.adjusted) and math.isfinite(p.sd) for p in f.projects)
    assert "flat" in f.judge("flat").flags
    assert "few-reviews" in f.judge("once").flags
    assert [p.project for p in f.projects] == ["p1", "p2", "p3"]
    # The textbook method silently zeroes the flat judge; ours keeps them as evidence of lean.
    assert zscore_means(obs)  # does not raise either, but see JUDGING.md


def test_fewer_reviews_widen_the_interval_rather_than_shrinking_the_score():
    obs = [Observation(f"j{i}", "many", 70 + (i % 3 - 1)) for i in range(6)]
    obs += [Observation("j0", "few", 70), Observation("j1", "few", 70)]
    f = fit(obs)
    assert f.project("few").sd > f.project("many").sd
    assert f.project("few").adjusted == pytest.approx(70, abs=2)


def test_one_review_per_project_is_handled():
    f = fit([Observation("j1", "p1", 50), Observation("j2", "p2", 70)])
    assert len(f.projects) == 2
    assert all(math.isfinite(p.adjusted) for p in f.projects)


def test_empty_and_identical_inputs():
    assert fit([]).projects == []
    f = fit([Observation("j1", "p1", 60), Observation("j2", "p1", 60), Observation("j1", "p2", 60)])
    assert all(p.adjusted == pytest.approx(60, abs=1e-6) for p in f.projects)


def test_p_above_next_is_a_probability_and_ordered():
    rng = np.random.default_rng(3)
    obs = [Observation(f"j{j}", f"p{p}", 10 * p + rng.normal(0, 1)) for p in range(5) for j in range(3)]
    f = fit(obs)
    assert [p.project for p in f.projects] == ["p4", "p3", "p2", "p1", "p0"]
    assert all(0.5 < p.p_above_next <= 1 for p in f.projects[:-1])
    assert f.projects[-1].p_above_next is None


def test_combine_respects_weights_and_ranges():
    crit = [
        {"key": "a", "weight": 3, "min_value": 1, "max_value": 5},
        {"key": "b", "weight": 1, "min_value": 1, "max_value": 10},
        {"key": "c", "weight": 0, "min_value": 1, "max_value": 5},
    ]
    assert combine({"a": 5, "b": 1}, crit) == pytest.approx(75.0)
    assert combine({"a": 1, "b": 10}, crit) == pytest.approx(25.0)
    assert combine({"b": 10}, crit) is None  # missing a weighted criterion
    assert combine({"a": 5, "b": 10, "c": 1}, crit) == pytest.approx(100.0)
