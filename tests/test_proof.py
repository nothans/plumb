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
