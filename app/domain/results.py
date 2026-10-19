"""Results, track leaders and awards."""

from __future__ import annotations

import sqlite3

from .. import normalize
from .core import (
    Actor,
    Forbidden,
)
from .events import (
    criteria,
    get_event,
    is_organizer,
    prizes,
)
from .judging import (
    reviews_for,
)
from .projects import (
    PROJECT_COLUMNS,
    PROJECT_FROM,
)


def compute_results(conn: sqlite3.Connection, event_id: str) -> dict:
    """Fit the normalization model on this event's eligible reviews.

    Excluded, and listed so the organizer can see it: reviews of duplicates,
    of disqualified projects, and of withdrawn drafts, plus any review that
    is missing a weighted criterion.
    """
    crit = criteria(conn, event_id)
    obs, excluded = [], []
    for r in reviews_for(conn, event_id=event_id):
        if r["duplicate_of"] or r["disqualified_reason"] or r["project_status"] != "submitted":
            excluded.append({"review": r["id"], "project": r["project_id"], "why": "project not eligible"})
            continue
        score = normalize.combine(r["values"], crit)
        if score is None:
            excluded.append({"review": r["id"], "project": r["project_id"], "why": "missing a weighted criterion"})
            continue
        obs.append(normalize.Observation(judge=r["judge_id"], project=r["project_id"], score=score))
    fit = normalize.fit(obs)
    projects = {r["id"]: r for r in conn.execute(
        f"SELECT {PROJECT_COLUMNS} {PROJECT_FROM} WHERE p.event_id = ?", (event_id,)
    )}
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT u.id, u.name FROM users u JOIN event_roles r ON r.user_id = u.id WHERE r.event_id = ? AND r.role = 'judge'",
        (event_id,),
    )}
    unreviewed = [p for pid, p in projects.items()
                  if p["status"] == "submitted" and not p["duplicate_of"] and not p["disqualified_reason"]
                  and pid not in {o.project for o in obs}]
    return {"fit": fit, "projects": projects, "names": names, "criteria": crit, "excluded": excluded,
            "unreviewed": unreviewed, "observations": len(obs)}


def track_leaders(fit, projects: dict) -> dict[str | None, dict]:
    """Per track: the leader, the runner-up, and how sure the model is that
    the leader is truly ahead. None is the key for the whole event."""
    out: dict[str | None, dict] = {}
    groups: dict[str | None, list] = {None: list(fit.projects)}
    for p in fit.projects:
        track = projects[p.project]["track_id"]
        if track is not None:
            groups.setdefault(track, []).append(p)
    for track, ranked in groups.items():
        if not ranked:
            continue
        lead = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        out[track] = {"leader": lead.project, "runner_up": second.project if second else None,
                      "p": fit.p_better(lead.project, second.project) if second else None,
                      "ranked": [p.project for p in ranked]}
    return out


# Under this chance of beating the best other candidate, the certainty tag
# reads "tie" and an award needs a written reason.
CLOSE_CALL = 0.6


def shown_percent(p: float) -> int:
    """A probability as the whole percentage every page shows. Bands (tie,
    leaning, likely, clear) and the close-call rule use this number, so the
    words never contradict the figure next to them."""
    return int(f"{100 * p:.0f}")
#
# There is deliberately no "would the winner survive losing any one judge"
# check: in simulated events, winners whose lead one judge's removal flips
# were as often truly ahead as those it did not (72% and 69% at a stated 60
# to 80%), and the check fired on most healthy wins. The probability already
# carries what it would add.


def award_check(fit, pick: str, group: list[str]) -> dict:
    """How safe it is to give a prize to `pick` out of `group` (the candidates,
    best first): the chance it truly beats the best other candidate. Under
    CLOSE_CALL it is a close call, which publishing accepts only with a
    reason on the record."""
    rival = next((p for p in group if p != pick), None)
    p = fit.p_better(pick, rival) if rival else None
    return {"rival": rival, "p": p,
            "close_call": rival is not None and (p is None or shown_percent(p) < 100 * CLOSE_CALL)}


def award_groups(fit, projects: dict, prize, overall_taken: set[str]) -> list[str]:
    """The candidates for a prize, best first: the track's projects for a
    track prize; for an overall prize, every project that has not already
    won an overall prize."""
    if prize["track_id"]:
        return [p.project for p in fit.projects if projects[p.project]["track_id"] == prize["track_id"]]
    return [p.project for p in fit.projects if p.project not in overall_taken]


def default_awards(conn: sqlite3.Connection, event_id: str, res: dict) -> list[dict]:
    """A suggested winner for every prize: a track prize goes to the track
    leader; overall prizes go down the overall ranking in prize order,
    skipping projects that already won an overall prize."""
    fit, projects = res["fit"], res["projects"]
    taken: set[str] = set()
    out = []
    for prize in prizes(conn, event_id):
        group = award_groups(fit, projects, prize, taken)
        pick = group[0] if group else None
        if pick and not prize["track_id"]:
            taken.add(pick)
        check = award_check(fit, pick, group) if pick else {"rival": None, "p": None, "close_call": False}
        out.append({"prize": prize, "project": pick, "runner_up": check["rival"], "p": check["p"],
                    "close_call": check["close_call"]})
    return out


def results_for_viewer(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """Organizers see live results at any time. Everyone else only after publication.

    After publication the ranking and its numbers are read back from the
    signed record, never recomputed: an upgrade that changed the model, or
    numpy, must not change what an event's page, CSV or API says it decided.
    """
    from .. import records
    event = get_event(conn, event_id)
    if not event["results_published_at"]:
        if not is_organizer(conn, event_id, actor):
            raise Forbidden("results are not published yet")
        return compute_results(conn, event_id)
    signed = records.published_results(conn, event_id)
    live = compute_results(conn, event_id)
    signed["fit"].judges = live["fit"].judges       # organizer diagnostics; the record carries no per-judge lean
    return {**live, "fit": signed["fit"], "projects": {**live["projects"], **signed["projects"]},
            "record": signed["record"], "body": signed["body"]}
