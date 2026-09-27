"""Results, track leaders and awards."""

from __future__ import annotations

import sqlite3

from .. import normalize
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Forbidden,
)
from .events import (  # noqa: F401
    criteria,
    get_event,
    is_organizer,
    prizes,
)
from .judging import (  # noqa: F401
    reviews_for,
)
from .projects import (  # noqa: F401
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


def default_awards(conn: sqlite3.Connection, event_id: str, fit, projects: dict) -> list[dict]:
    """A suggested winner for every prize: a track prize goes to the track
    leader; overall prizes go down the overall ranking in prize order,
    skipping projects that already won an overall prize."""
    leaders = track_leaders(fit, projects)
    overall = [p.project for p in fit.projects]
    taken: set[str] = set()
    out = []
    for prize in prizes(conn, event_id):
        if prize["track_id"]:
            info = leaders.get(prize["track_id"])
            pick = info["leader"] if info else None
            runner = info["runner_up"] if info else None
        else:
            pool = [p for p in overall if p not in taken]
            pick = pool[0] if pool else None
            runner = pool[1] if len(pool) > 1 else None
            if pick:
                taken.add(pick)
        out.append({"prize": prize, "project": pick, "runner_up": runner,
                    "p": fit.p_better(pick, runner) if pick and runner else None})
    return out


def results_for_viewer(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """Organizers see live results at any time. Everyone else only after publication."""
    event = get_event(conn, event_id)
    if not event["results_published_at"] and not is_organizer(conn, event_id, actor):
        raise Forbidden("results are not published yet")
    return compute_results(conn, event_id)
