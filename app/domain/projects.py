"""Projects: drafts, submission, the gallery, duplicates and disqualification."""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import new_id, now, transaction
from .core import (
    Actor,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    clean_text,
    clean_url,
)
from .events import (
    get_event,
    is_organizer,
    require_organizer,
    require_user,
)
from .teams import (
    submissions_state,
    team_for,
)

PROJECT_COLUMNS = (
    "p.*, t.name AS team_name, tr.name AS track_name, e.name AS event_name"
)
PROJECT_FROM = (
    "FROM projects p JOIN teams t ON t.id = p.team_id JOIN events e ON e.id = p.event_id "
    "LEFT JOIN tracks tr ON tr.id = p.track_id"
)

# A live entry: submitted, not superseded by a duplicate, not disqualified.
# Only live entries are in the gallery, the API, the widget, the counts, the
# ballot, the judge queues, assignment and the ranking. Defined once, here,
# for the projects table under the alias p; live_entry("x") for another alias.
def live_entry(alias: str = "p") -> str:
    return (f"{alias}.status = 'submitted' AND {alias}.duplicate_of IS NULL "
            f"AND {alias}.disqualified_reason IS NULL")


LIVE_ENTRY = live_entry("p")


def is_live(project) -> bool:
    return project["status"] == "submitted" and not project["duplicate_of"] and not project["disqualified_reason"]


def live_count(conn: sqlite3.Connection, event_id: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM projects p WHERE p.event_id = ? AND {LIVE_ENTRY}", (event_id,)).fetchone()[0]


def _utc(ts: str) -> str:
    """"2026-03-01T18:00:00Z" -> "2026-03-01 18:00 UTC", the way the pages print times."""
    return f"{ts[:10]} {ts[11:16]} UTC" if ts else ""


def get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT {PROJECT_COLUMNS} {PROJECT_FROM} WHERE p.id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFound("no such project")
    return row


def can_view_project(conn: sqlite3.Connection, project: sqlite3.Row, actor: Actor | None) -> bool:
    if project["status"] == "submitted" and project["duplicate_of"] is None:
        return True
    if actor is None:
        return False
    if is_organizer(conn, project["event_id"], actor):
        return True
    return conn.execute(
        "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (project["team_id"], actor.id)
    ).fetchone() is not None


def view_project(conn: sqlite3.Connection, actor: Actor | None, project_id: str) -> sqlite3.Row:
    project = get_project(conn, project_id)
    if not can_view_project(conn, project, actor):
        # Same answer as a missing project, so drafts do not leak their existence.
        raise NotFound("no such project")
    return project


def _judged(conn: sqlite3.Connection, project_id: str) -> bool:
    """Whether any judge has reviewed or compared this entry."""
    return conn.execute("SELECT 1 FROM reviews WHERE project_id = ? UNION ALL SELECT 1 FROM comparisons "
                        "WHERE project_a = ? OR project_b = ? LIMIT 1", (project_id, project_id, project_id)).fetchone() is not None


def save_project(conn: sqlite3.Connection, actor: Actor | None, event_id: str, form: dict,
                 *, submit: bool, project_id: str | None = None) -> str:
    """Create or edit the actor's team project.

    The deadline check comes first, straight after authentication: after the
    close, nothing about a submission changes, whatever else is wrong with
    the request.
    """
    actor = require_user(actor)
    with transaction(conn):
        event = get_event(conn, event_id)
        team = team_for(conn, event_id, actor.id)
        state, closes = submissions_state(conn, event, team["id"] if team else None)
        if state == "closed":
            raise Conflict(f"submissions for {event['name']} closed at {_utc(closes)}; entries are final")
        if state == "upcoming":
            raise Conflict(f"submissions for {event['name']} open at {_utc(event['submissions_open'])}")
        if team is None:
            raise Forbidden("join or create a team for this event before submitting")
        title = clean_text(form.get("title"), "title", required=True, max_len=120)
        values = {
            "title": title,
            "summary": clean_text(form.get("summary"), "summary", max_len=280),
            "description": clean_text(form.get("description"), "description", max_len=10000),
            "repo_url": clean_url(form.get("repo_url"), "repository URL"),
            "demo_url": clean_url(form.get("demo_url"), "demo URL"),
            "track_id": (form.get("track_id") or None),
        }
        if values["track_id"] and not conn.execute(
            "SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (values["track_id"], event_id)
        ).fetchone():
            raise Invalid("that track is not part of this event")
        if submit and not values["summary"]:
            raise Invalid("add a one-line summary before submitting")
        existing = conn.execute(
            "SELECT * FROM projects WHERE team_id = ? AND duplicate_of IS NULL", (team["id"],)
        ).fetchone()
        if project_id and (existing is None or existing["id"] != project_id):
            raise Forbidden("that is not your team's project")
        if existing and _judged(conn, existing["id"]):
            # Only possible on an extension: judges scored what they saw.
            raise Conflict("a judge has already reviewed this entry, so it can no longer change")
        ts = now()
        if existing:
            pid = existing["id"]
            status = "submitted" if submit else existing["status"]
            submitted_at = existing["submitted_at"] if existing["status"] == "submitted" else (ts if submit else None)
            conn.execute(
                "UPDATE projects SET title=?, summary=?, description=?, repo_url=?, demo_url=?, track_id=?, "
                "status=?, submitted_at=?, updated_at=? WHERE id=?",
                (*values.values(), status, submitted_at, ts, pid),
            )
            action = "project.submitted" if submit and existing["status"] == "draft" else "project.edited"
        else:
            pid = new_id("prj")
            conn.execute(
                "INSERT INTO projects(id, event_id, team_id, title, summary, description, repo_url, demo_url, track_id, "
                "status, submitted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, event_id, team["id"], *values.values(), "submitted" if submit else "draft",
                 ts if submit else None, ts, ts),
            )
            action = "project.submitted" if submit else "project.drafted"
        audit.append(conn, action, actor_id=actor.id, event_id=event_id, subject=pid,
                     detail={"title": title, "track_id": values["track_id"]}, ip=actor.ip)
    return pid


def unsubmit_project(conn: sqlite3.Connection, actor: Actor | None, project_id: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        project = get_project(conn, project_id)
        event = get_event(conn, project["event_id"])
        if submissions_state(conn, event, project["team_id"])[0] != "open":
            raise Conflict("submissions are closed")
        if _judged(conn, project_id):
            raise Conflict("a judge has already reviewed this entry, so it can no longer change")
        if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (project["team_id"], actor.id)).fetchone():
            raise Forbidden("that is not your team's project")
        conn.execute("UPDATE projects SET status='draft', submitted_at=NULL, updated_at=? WHERE id=?", (now(), project_id))
        audit.append(conn, "project.unsubmitted", actor_id=actor.id, event_id=event["id"], subject=project_id, ip=actor.ip)


def gallery(conn: sqlite3.Connection, *, event_id: str | None = None, track_id: str | None = None,
            q: str | None = None, limit: int = 60, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    where = [LIVE_ENTRY]
    args: list = []
    if event_id:
        where.append("p.event_id = ?")
        args.append(event_id)
    else:
        where.append("e.archived_at IS NULL")
    if track_id:
        where.append("p.track_id = ?")
        args.append(track_id)
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(p.title LIKE ? ESCAPE '\\' OR p.summary LIKE ? ESCAPE '\\' OR t.name LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) {PROJECT_FROM} WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        # The newest event first, then projects in id order: stable across
        # pages and across restarts.
        f"SELECT {PROJECT_COLUMNS} {PROJECT_FROM} WHERE {clause} ORDER BY e.created_at DESC, e.id DESC, p.id "
        "LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return rows, total


def set_disqualified(conn: sqlite3.Connection, actor: Actor | None, project_id: str, reason: str | None) -> None:
    project = get_project(conn, project_id)
    actor = require_organizer(conn, project["event_id"], actor)
    reason = clean_text(reason, "reason", max_len=300) or None
    with transaction(conn):
        if get_event(conn, project["event_id"])["results_published_at"]:
            raise Conflict("results are published; entries can no longer be disqualified or reinstated")
        conn.execute("UPDATE projects SET disqualified_reason = ? WHERE id = ?", (reason, project_id))
        audit.append(conn, "project.disqualified" if reason else "project.requalified", actor_id=actor.id,
                     event_id=project["event_id"], subject=project_id, detail={"reason": reason}, ip=actor.ip)


def duplicate_candidates(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    """Pairs that look like the same entry twice: same team, same repo, or same title."""
    rows = conn.execute(
        f"SELECT {PROJECT_COLUMNS} {PROJECT_FROM} WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY p.submitted_at",
        (event_id,),
    ).fetchall()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            reasons = []
            if a["team_id"] == b["team_id"]:
                reasons.append("same team")
            if a["repo_url"] and a["repo_url"].rstrip("/").lower() == b["repo_url"].rstrip("/").lower():
                reasons.append("same repository")
            if a["title"].strip().lower() == b["title"].strip().lower():
                reasons.append("same title")
            if reasons:
                resolved = b["duplicate_of"] == a["id"] or a["duplicate_of"] == b["id"]
                # Confirmed means a person decided, not just the importer's default.
                confirmed = resolved and conn.execute(
                    "SELECT 1 FROM audit_log WHERE event_id = ? AND action = 'project.marked_duplicate' AND subject IN (?, ?)",
                    (event_id, a["id"], b["id"])).fetchone() is not None
                out.append({"first": a, "second": b, "reasons": reasons, "resolved": resolved, "confirmed": confirmed})
    return out


def resolve_duplicate(conn: sqlite3.Connection, actor: Actor | None, keep_id: str, drop_id: str) -> None:
    """Mark drop_id as a duplicate of keep_id. The duplicate stays in the
    database (and in exports) for the record, but leaves the gallery and the
    ranking. Reviews of the dropped entry are not moved: a judge scored what
    they saw, and silently re-attributing a score would be worse than losing it."""
    keep, drop = get_project(conn, keep_id), get_project(conn, drop_id)
    if keep["event_id"] != drop["event_id"] or keep_id == drop_id:
        raise Invalid("pick two different projects from the same event")
    pair = {keep_id, drop_id}
    if not any({d["first"]["id"], d["second"]["id"]} == pair for d in duplicate_candidates(conn, keep["event_id"])):
        raise Invalid("those two do not share a team, repository or title; to remove an entry, disqualify it with a reason")
    actor = require_organizer(conn, keep["event_id"], actor)
    with transaction(conn):
        if get_event(conn, keep["event_id"])["results_published_at"]:
            raise Conflict("results are published; duplicates can no longer be changed")
        live = conn.execute(
            "SELECT id FROM projects WHERE team_id = ? AND duplicate_of IS NULL AND id NOT IN (?, ?)",
            (keep["team_id"], keep_id, drop_id),
        ).fetchone()
        if live:
            raise Conflict(f"{keep['team_name']} already has a live entry ({live['id']}); resolve that pair first")
        # Order matters: a team may never have two live entries, even for
        # the length of one statement. Retire the dropped one first, then
        # repoint anything that pointed at it, then make keep live.
        conn.execute("UPDATE projects SET duplicate_of = ? WHERE id = ?", (keep_id, drop_id))
        conn.execute("UPDATE projects SET duplicate_of = ? WHERE duplicate_of = ? AND id <> ?", (keep_id, drop_id, keep_id))
        conn.execute("UPDATE projects SET duplicate_of = NULL WHERE id = ?", (keep_id,))
        audit.append(conn, "project.marked_duplicate", actor_id=actor.id, event_id=keep["event_id"],
                     subject=drop_id, detail={"kept": keep_id}, ip=actor.ip)
