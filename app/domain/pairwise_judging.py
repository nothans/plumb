"""Pairwise (Bradley-Terry) judging."""

from __future__ import annotations

import sqlite3

from .. import audit, normalize, pairwise
from ..db import now, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Conflict,
    Forbidden,
    Invalid,
)
from .events import (  # noqa: F401
    criteria,
    get_event,
    phase,
    require_judge,
    require_user,
    roles,
)
from .judging import (  # noqa: F401
    judges,
    reviews_for,
)
from .projects import (  # noqa: F401
    get_project,
)
from .results import (  # noqa: F401
    results_for_viewer,
)


def _eligible_assigned(conn: sqlite3.Connection, event_id: str, judge_id: str) -> list[str]:
    return [r["id"] for r in conn.execute(
        "SELECT p.id FROM assignments a JOIN projects p ON p.id = a.project_id WHERE a.event_id = ? AND a.judge_id = ? "
        "AND p.status = 'submitted' AND p.duplicate_of IS NULL AND p.disqualified_reason IS NULL ORDER BY p.id",
        (event_id, judge_id),
    )]


def _direct_comparisons(conn: sqlite3.Connection, event_id: str) -> list[pairwise.Comparison]:
    out = []
    for r in conn.execute(
        "SELECT c.* FROM comparisons c JOIN projects a ON a.id = c.project_a JOIN projects b ON b.id = c.project_b "
        "WHERE c.event_id = ? AND a.duplicate_of IS NULL AND b.duplicate_of IS NULL "
        "AND a.status = 'submitted' AND b.status = 'submitted' "
        "AND a.disqualified_reason IS NULL AND b.disqualified_reason IS NULL", (event_id,)
    ):
        loser = r["project_b"] if r["winner"] == r["project_a"] else r["project_a"]
        out.append(pairwise.Comparison(r["winner"], loser))
    return out


def pairwise_next(conn: sqlite3.Connection, actor: Actor | None, event_id: str, skip: set | None = None) -> dict:
    """The next pair for this judge, chosen where the ranking is least sure."""
    actor = require_judge(conn, event_id, actor)
    event = get_event(conn, event_id)
    if not event["pairwise"]:
        raise Conflict("pairwise judging is not enabled for this event")
    assigned = _eligible_assigned(conn, event_id, actor.id)
    done = {frozenset((r["project_a"], r["project_b"])) for r in conn.execute(
        "SELECT project_a, project_b FROM comparisons WHERE judge_id = ? AND event_id = ?", (actor.id, event_id)
    )}
    # Pair choice uses only this judge's own comparisons. Using everyone's
    # would let the order in which pairs appear hint at peers' judgments.
    own = [pairwise.Comparison(r["winner"], r["project_b"] if r["winner"] == r["project_a"] else r["project_a"])
           for r in conn.execute("SELECT * FROM comparisons WHERE judge_id = ? AND event_id = ?", (actor.id, event_id))]
    current = pairwise.fit(own) if own else None
    pair = pairwise.next_pair(assigned, done | (skip or set()), current)
    n = len(assigned)
    return {
        "event": event,
        "pair": (get_project(conn, pair[0]), get_project(conn, pair[1])) if pair else None,
        "done": sum(1 for pair in done if pair <= set(assigned)),
        "possible": n * (n - 1) // 2,
    }


def record_comparison(conn: sqlite3.Connection, actor: Actor | None, event_id: str, a: str, b: str, winner: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        if "judge" not in roles(conn, event_id, actor):
            raise Forbidden("only this event's judges can compare projects")
        event = get_event(conn, event_id)
        if not event["pairwise"]:
            raise Conflict("pairwise judging is not enabled for this event")
        if not phase(event)["judging_open"]:
            raise Conflict("judging is not open for this event")
        if a == b or winner not in (a, b):
            raise Invalid("pick one of two different projects")
        assigned = set(_eligible_assigned(conn, event_id, actor.id))
        if a not in assigned or b not in assigned:
            raise Forbidden("you can only compare projects assigned to you")
        lo, hi = sorted((a, b))
        try:
            conn.execute(
                "INSERT INTO comparisons(event_id, judge_id, project_a, project_b, winner, created_at) VALUES (?,?,?,?,?,?)",
                (event_id, actor.id, lo, hi, winner, now()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("you already compared these two") from exc
        audit.append(conn, "comparison.recorded", actor_id=actor.id, event_id=event_id, subject=winner,
                     detail={"a": lo, "b": hi, "winner": winner}, ip=actor.ip)


def pairwise_results(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """Bradley-Terry over direct comparisons, plus the same estimator over
    the preferences implied by rubric scores, and how both agree with the
    normalized ranking. Visibility follows the main results."""
    res = results_for_viewer(conn, actor, event_id)
    ranked = [p.project for p in res["fit"].projects]
    direct = _direct_comparisons(conn, event_id)
    crit = res["criteria"]
    obs = []
    for r in reviews_for(conn, event_id=event_id):
        if r["duplicate_of"] or r["disqualified_reason"] or r["project_status"] != "submitted":
            continue
        score = normalize.combine(r["values"], crit)
        if score is not None:
            obs.append(normalize.Observation(r["judge_id"], r["project_id"], score))
    implied = pairwise.fit(pairwise.implied(obs), ranked) if obs else None
    direct_fit = pairwise.fit(direct, ranked) if direct else None

    def agreement(bt) -> float | None:
        """Spearman's rho between the two orders, over the projects both rank."""
        if bt is None:
            return None
        order_b = [x.project for x in bt.strengths if x.comparisons > 0]
        common = set(ranked) & set(order_b)
        a = [p for p in ranked if p in common]
        b = {p: i for i, p in enumerate(q for q in order_b if q in common)}
        n = len(a)
        if n < 3:
            return None
        d2 = sum((i - b[p]) ** 2 for i, p in enumerate(a))
        return 1 - 6 * d2 / (n * (n * n - 1))

    return {"direct": direct_fit, "implied": implied, "n_direct": len(direct),
            "agree_direct": agreement(direct_fit), "agree_implied": agreement(implied), "projects": res["projects"]}
