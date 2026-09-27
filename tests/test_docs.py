"""The numbers the documents quote must be the numbers the code produces."""

import json
import re
from collections import defaultdict

from app.normalize import Observation, combine, fit

from .conftest import ROOT

JUDGING = (ROOT / "JUDGING.md").read_text(encoding="utf-8")


def _fixture_fit():
    d = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))
    crit = [{"key": k, "weight": 1, "min_value": 1, "max_value": 5} for k in ("functionality", "quality", "innovation")]
    obs = [Observation(s["judge"], s["project"], combine(s["criteria"], crit))
           for s in d["scores"] if s["project"] != "prj_07"]  # prj_07 is the superseded duplicate
    return d, fit(obs)


def test_section_6_figures_match_the_fixture_fit():
    d, f = _fixture_fit()
    lean = [p.raw_mean - p.adjusted for p in f.projects]
    moved = sum(1 for p in f.projects if p.rank != p.raw_rank)
    half = [p.high - p.adjusted for p in f.projects]
    quoted = {
        "121 reviews": f.n_observations == 121,
        f"plus or minus {f.judge_sd:.1f} points": True,
        f"plus or minus {f.noise_sd:.1f} points": True,
        f"moves {moved} of 40 projects": True,
        f"at most {max(abs(p.rank - p.raw_rank) for p in f.projects)} places": True,
        f"between {min(lean):.1f} and +{max(lean):.1f} points": True,
    }
    for text, ok in quoted.items():
        assert ok and text in JUDGING, text
    assert f.spread_sd < 0.5 and "no detectable true spread" in JUDGING
    assert f"{min(half):.1f} to {max(half):.0f} points wide" in JUDGING
    tracks = {p["id"]: p["track"] for p in d["projects"]}
    groups = defaultdict(list)
    for p in f.projects:
        groups[tracks[p.project]].append(p.project)
    assert all(abs(f.p_better(v[0], v[1]) - 0.5) < 0.01 for v in groups.values() if len(v) > 1)
    assert all(abs(p.p_above_next - 0.5) < 0.01 for p in f.projects[:-1])


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
