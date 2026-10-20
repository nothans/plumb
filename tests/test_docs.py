"""The numbers the documents quote must be the numbers the code produces."""

import json
import re

from app.normalize import Observation, combine, fit

from .conftest import ROOT

JUDGING = (ROOT / "JUDGING.md").read_text(encoding="utf-8")


def _fixture_fit():
    d = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))
    crit = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("functionality", "quality", "innovation")]
    obs = [Observation(s["judge"], s["project"], combine(s["criteria"], crit)) for s in d["scores"]]
    # As the portal fits it: prj_07, the superseded duplicate, calibrates its judges but is not ranked.
    return d, fit(obs, ranked={p["id"] for p in d["projects"]} - {"prj_07"})


def test_section_6_figures_match_the_fixture_fit():
    d, f = _fixture_fit()
    lean = [p.raw_mean - p.adjusted for p in f.projects]
    moved = sum(1 for p in f.projects if p.rank != p.raw_rank)
    half = [p.high - p.adjusted for p in f.projects]
    quoted = {
        "all 126 reviews": f.n_observations == 126,
        f"plus or minus {f.judge_sd:.1f} points": True,
        f"plus or minus {f.noise_sd:.1f} points": True,
        f"moves {moved} of 40 projects": True,
        f"at most {max(abs(p.rank - p.raw_rank) for p in f.projects)} places": True,
        f"between {min(lean):.1f} and +{max(lean):.1f} points": True,
    }
    for text, ok in quoted.items():
        assert ok and text in JUDGING, text
    sp = f.spread_summary
    assert sp["zero_plausible"] and f"spread between projects of about {sp['estimate']:.1f} points, anywhere from "         f"{sp['low90']:.0f} to {sp['high90']:.1f} (90%)" in JUDGING
    assert f"{min(half):.1f} to {max(half):.0f} points wide" in JUDGING
    beats = [p.p_above_next for p in f.projects if p.p_above_next is not None]
    assert f"each \"beats next\" is between {min(beats):.0%} and {max(beats):.0%}" in JUDGING
    assert f"({f.dof} degrees of freedom) the multiplier is {f.projects[0].q90:.2f}" in JUDGING
    assert "all four of the replay event's prizes are close calls" in JUDGING


def test_the_replay_prizes_are_the_close_calls_the_docs_describe(db):
    from app import domain
    suggested = domain.default_awards(db, "evt_replay", domain.compute_results(db, "evt_replay"))
    assert len(suggested) == 4 and all(s["close_call"] for s in suggested)
    record = db.execute("SELECT payload FROM records WHERE event_id = 'evt_replay' AND kind = 'results'").fetchone()[0]
    assert sum(1 for a in json.loads(record)["awards"] if a["close_call"] and a["reason"]) == 4


def test_the_organizers_sigma_is_reproduced():
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import normalization_proof as proof
    d, _ = _fixture_fit()
    assert round(proof.judge_spread(d["scores"]), 2) == 0.42
    assert "which is 0.42" in JUDGING and "takes it to 0.37" in JUDGING
    crit = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("functionality", "quality", "innovation")]
    f = fit([Observation(s["judge"], s["project"], combine(s["criteria"], crit)) for s in d["scores"]])
    lean = {j.judge: j.leniency for j in f.judges}
    assert round(proof.judge_spread(d["scores"], lean), 2) == 0.37


def test_judging_table_matches_the_committed_proof_output():
    proof = (ROOT / "docs" / "normalization-proof.md").read_text(encoding="utf-8")
    for line in re.findall(r"^\| (\S+) \| ([\d.]+) \| ([\d.]+) \| \*{0,2}([\d.]+)\*{0,2} \| ([\d.]+) \|", JUDGING, re.M):
        scenario, raw, z, plumb, pw = line
        for method, value in (("raw", raw), ("zscore", z), ("plumb", plumb), ("pairwise", pw)):
            assert re.search(rf"^\| {scenario} \| {method} \| [\d.]+ \| {re.escape(value)} \|", proof, re.M), (scenario, method)


def test_every_extended_check_is_mapped_to_a_spec_bullet_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme.split("### Every T3 and T4 bullet")[1].split("**Bonus")[0]
    source = (ROOT / "tools" / "acceptance_extended.py").read_text(encoding="utf-8")
    names = [line.split('", "', 1)[1].split('"', 1)[0] for line in source.splitlines()
             if 'rep.check("T' in line and '", "' in line]
    assert len(names) >= 40
    assert [n for n in names if n not in table] == []


def test_the_readme_tier_table_matches_the_committed_reports():
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import readme_tiers
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tests = int(readme.split("| All of it | ")[1].split(" tests")[0])   # the count itself is checked in CI
    assert readme_tiers.render(readme, readme_tiers.table(tests=tests)) == readme
