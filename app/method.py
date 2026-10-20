"""The judging method, written down and hashed, so it can be registered
before anyone sees a ranking and checked against what produced the results.

Locking an event's rubric registers the method: this description is stored
with the event, its hash is published on the event page, and the database
refuses to change either afterwards (rule_method_registered_once). The
signed results carry the method that produced them and say whether it is
the one registered, so a reweighting, a changed threshold or an upgraded
model between the lock and publication shows up instead of passing quietly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import canonical, normalize

# Under this chance of beating the best other candidate, the certainty tag
# reads "tie" and an award needs a written reason.
CLOSE_CALL = 0.6

MODEL = ("score = quality[project] + leniency[judge] + noise; projects fixed, judges random; "
         "REML for the judge-to-noise ratio")
_MODEL_SOURCE = hashlib.sha256(Path(normalize.__file__).read_bytes()).hexdigest()


def describe(criteria: list) -> dict:
    """Everything that decides a ranking and its awards, given a rubric."""
    return {
        "rubric": [{"key": c["key"], "weight": float(c["weight"]), "min": c["min_value"], "max": c["max_value"]}
                   for c in sorted(criteria, key=lambda c: c["key"])],
        "combine": "weighted mean of each criterion mapped onto 0..1 by its range, reported on 0 to 100",
        "model": MODEL,
        "model_source_sha256": _MODEL_SOURCE,
        "intervals": "90%, Student's t with reviews minus projects degrees of freedom",
        "probabilities": "averaged over the likelihoods of the judge-to-noise ratio and of the spread of true quality",
        "probabilities_withheld_below_dof": normalize.MIN_DOF,
        "close_call_below": CLOSE_CALL,
        "close_call_rule": "a prize under the line, or with no probability, needs the organizers' signed reason",
        "ties": "equal adjusted scores are ordered by project id",
        "unranked_entries": "duplicates, disqualified entries and withdrawn drafts are fitted but not ranked",
    }


def digest(method: dict) -> str:
    return hashlib.sha256(canonical.dumps(method).encode()).hexdigest()
