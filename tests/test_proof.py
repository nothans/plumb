"""A small, seeded run of the normalization proof, so the claims in
JUDGING.md cannot silently stop being true."""

import sys

from .conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

import normalization_proof as proof  # noqa: E402


def test_headline_ordering_holds():
    res = proof.evaluate(draws=80, seed=11, scenarios=("additive", "track", "no-bias"))
    for scenario in ("additive", "track"):
        r = res[scenario]
        assert r["plumb"]["spearman"] > r["raw"]["spearman"] > r["zscore"]["spearman"], scenario
        assert r["plumb"]["vs_raw"] - r["plumb"]["vs_raw_ci"] > 0, scenario
    # Correcting for judges costs nothing when judges are fair.
    assert abs(res["no-bias"]["plumb"]["spearman"] - res["no-bias"]["raw"]["spearman"]) < 0.005
    # The intervals mean what they say.
    for scenario in res:
        assert 0.86 <= res[scenario]["plumb"]["coverage90"] <= 0.94, scenario


def test_beats_next_probabilities_are_calibrated():
    res = proof.evaluate(draws=80, seed=5, scenarios=("additive", "fixture"))
    for scenario, r in res.items():
        for b in r["calibration"]:
            if b["n"] >= 300:  # enough pairs for the bin to mean something
                assert abs(b["stated"] - b["observed"]) < 0.06, (scenario, b)


def test_small_events_are_why_probabilities_are_withheld():
    """Below MIN_DOF the probabilities run high (so the portal withholds
    them); on a larger event with real differences they match."""
    small = proof.small_events(draws=300, seed=3, designs=[(10, 5, 2)], spreads=(4.0, 8.0))
    assert all(r["dof"] < proof.MIN_DOF and r["few_reviews"] == 1.0 for r in small)
    high = [b for r in small for b in r["bins"] if b["bin"] in ("75%-90%", "90%-100%") and b["n"]]
    n = sum(b["n"] for b in high)
    stated = sum(b["stated"] * b["n"] for b in high) / n
    observed = sum(b["observed"] * b["n"] for b in high) / n
    assert n >= 150 and stated - observed > 0.08, (n, stated, observed)
    (large,) = proof.small_events(draws=60, seed=3, designs=[(40, 10, 3)], spreads=(16.0,))
    assert large["dof"] >= proof.MIN_DOF and large["few_reviews"] == 0.0
    mid = next(b for b in large["bins"] if b["bin"] == "60%-75%")
    assert mid["n"] >= 100 and abs(mid["stated"] - mid["observed"]) < 0.06, mid
