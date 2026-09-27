import numpy as np

from app import pairwise as pw
from app.normalize import Observation

from .conftest import post_form


def test_recovers_planted_strengths():
    rng = np.random.default_rng(0)
    true = {f"p{i}": v for i, v in enumerate(rng.normal(0, 1.5, 15))}
    names = list(true)
    comps = []
    for _ in range(1500):
        a, b = rng.choice(names, 2, replace=False)
        p = 1 / (1 + np.exp(-(true[a] - true[b])))
        comps.append(pw.Comparison(a, b) if rng.random() < p else pw.Comparison(b, a))
    f = pw.fit(comps)
    est = np.array([f.get(p).strength for p in names])
    assert np.corrcoef(est, [true[p] for p in names])[0, 1] > 0.98


def test_undefeated_project_stays_finite_and_uncertain_projects_have_wide_intervals():
    comps = [pw.Comparison("a", "b"), pw.Comparison("a", "c"), pw.Comparison("b", "c")] * 5
    comps.append(pw.Comparison("d", "c"))
    f = pw.fit(comps, ["a", "b", "c", "d", "e"])
    assert [s.project for s in f.strengths][0] == "a"
    assert np.isfinite(f.get("a").strength)
    assert f.get("d").sd > f.get("b").sd  # one comparison vs fifteen
    assert f.get("e").comparisons == 0 and abs(f.get("e").strength) < 1e-9  # never compared: prior mean
    assert 0.5 < f.p_better("a", "c") <= 1


def test_implied_preferences_ignore_scale_and_ties_carry_nothing():
    harsh = [Observation("h", "x", 20), Observation("h", "y", 10)]
    stretched = [Observation("s", "x", 95), Observation("s", "y", 5)]
    flat = [Observation("f", "x", 70), Observation("f", "y", 70)]
    assert pw.implied(harsh) == pw.implied(stretched) == [pw.Comparison("x", "y")]
    tie = pw.implied(flat)
    assert sorted((c.winner, c.weight) for c in tie) == [("x", 0.5), ("y", 0.5)]


def test_next_pair_skips_done_pairs_and_prefers_uncertain_ones():
    f = pw.fit([pw.Comparison("a", "b")] * 10 + [pw.Comparison("c", "d")])
    assert pw.next_pair(["a", "b", "c", "d"], set(), f) in {("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "d")}
    assert pw.next_pair(["a", "b"], {frozenset(("a", "b"))}, f) is None


def test_judge_compares_and_organizer_sees_bradley_terry(judge_a, organizer, judge_b, db):
    nxt = judge_a.get("/api/v1/events/evt_01/compare/next").json()
    a, b = (p["id"] for p in nxt["pair"])
    assert nxt["possible"] == 55  # 11 assigned projects
    r = judge_a.post("/api/v1/events/evt_01/comparisons", json={"a": a, "b": b, "winner": b})
    assert r.status_code == 204
    assert judge_a.post("/api/v1/events/evt_01/comparisons", json={"a": b, "b": a, "winner": a}).status_code == 409
    after = judge_a.get("/api/v1/events/evt_01/compare/next").json()
    assert {p["id"] for p in after["pair"]} != {a, b} and after["done"] == 1

    # Only assigned projects, only judges, and results stay organizer-only.
    other = db.execute(
        "SELECT id FROM projects WHERE event_id = 'evt_01' AND duplicate_of IS NULL AND id NOT IN "
        "(SELECT project_id FROM assignments WHERE judge_id = 'jdg_24') LIMIT 1").fetchone()[0]
    assert judge_a.post("/api/v1/events/evt_01/comparisons", json={"a": a, "b": other, "winner": a}).status_code == 403
    assert judge_b.get("/api/v1/events/evt_01/pairwise").status_code == 403

    res = organizer.get("/api/v1/events/evt_01/pairwise").json()
    assert res["comparisons"] == 1
    assert res["direct"][0]["project_id"] == b
    assert -1 <= res["agreement_with_normalized"]["implied"] <= 1
    assert "Cross-checks" in organizer.get("/events/evt_01/manage/results").text

    # The HTML flow works too.
    page = judge_a.get("/events/evt_01/compare").text
    assert "is better" in page


def test_pairwise_toggle_in_settings(organizer, db):
    form = {k: db.execute(f"SELECT {k} FROM events WHERE id = 'evt_demo_live'").fetchone()[0] or ""
            for k in ("name", "submissions_open", "submissions_close", "judging_close")}
    r = post_form(organizer, "/events/evt_demo_live/manage/settings", {**form, "pairwise": ["0", "1"]},
                  csrf_from="/events/evt_demo_live/manage/settings")
    assert r.status_code == 303
    assert db.execute("SELECT pairwise FROM events WHERE id = 'evt_demo_live'").fetchone()[0] == 1
    r = post_form(organizer, "/events/evt_demo_live/manage/settings", {**form, "pairwise": "0"},
                  csrf_from="/events/evt_demo_live/manage/settings")
    assert db.execute("SELECT pairwise FROM events WHERE id = 'evt_demo_live'").fetchone()[0] == 0
