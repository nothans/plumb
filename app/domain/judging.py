"""Judges, invitations, assignment and reviews."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import assign, audit
from ..db import new_id, now, parse_ts, transaction
from ..security import new_token, token_hash
from .core import (
    Actor,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    _utcnow,
    clean_email,
    clean_text,
    get_user,
    user_by_email,
)
from .events import (
    criteria,
    get_event,
    is_organizer,
    phase,
    require_judge,
    require_organizer,
    require_user,
    roles,
    tracks,
)
from .projects import (
    PROJECT_COLUMNS,
    PROJECT_FROM,
    get_project,
    live_entry,
)
from .teams import (
    team_for,
)


def invite(conn: sqlite3.Connection, actor: Actor | None, event_id: str, email: str, role: str) -> str:
    """Returns the raw invitation token. Plumb does not send mail; the
    organizer copies the link, which keeps the portal fully offline."""
    actor = require_organizer(conn, event_id, actor)
    email = clean_email(email)
    if role not in ("judge", "organizer"):
        raise Invalid("role must be judge or organizer")
    token = new_token(24)
    with transaction(conn):
        conn.execute(
            "INSERT INTO invitations(token_hash, event_id, email, role, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (token_hash(token), event_id, email, role, actor.id, now()),
        )
        audit.append(conn, "invitation.created", actor_id=actor.id, event_id=event_id, subject=email,
                     detail={"role": role}, ip=actor.ip)
    return token


INVITATION_DAYS = 14


def get_invitation(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT i.*, e.name AS event_name FROM invitations i JOIN events e ON e.id = i.event_id WHERE token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if row is None:
        raise NotFound("that invitation link is not valid")
    if row["accepted_at"] is None and parse_ts(row["created_at"]) < _utcnow() - timedelta(days=INVITATION_DAYS):
        raise Conflict(f"this invitation expired after {INVITATION_DAYS} days; ask the organizer for a new one")
    return row


def events_of(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Every event an account belongs to, as a judge, organizer or team member."""
    return [r[0] for r in conn.execute(
        "SELECT event_id FROM event_roles WHERE user_id = ? UNION SELECT event_id FROM team_members WHERE user_id = ?",
        (user_id, user_id))]


def _vouches_for(conn: sqlite3.Connection, voucher_id: str | None, user_id: str, *, besides: str | None = None) -> bool:
    """Whether voucher runs every event the account belongs to (besides the
    one being joined). Setting an unclaimed account's password hands over
    all of its roles, so only someone who organizes all of them may vouch."""
    voucher = get_user(conn, voucher_id) if voucher_id else None
    if voucher is None:
        return False
    who = Actor(id=voucher["id"], email=voucher["email"], name=voucher["name"], is_admin=bool(voucher["is_admin"]))
    return all(is_organizer(conn, e, who) for e in events_of(conn, user_id) if e != besides)


def may_claim_with(conn: sqlite3.Connection, inv: sqlite3.Row) -> bool:
    """Whether an invitation may set the password of the account it names.

    Holding the link proves the organizer vouches for the address. For an
    unclaimed account that already belongs to other events (imported there
    as a judge or team member), the organizer who made the invitation must
    also run every one of those events; otherwise an organizer anywhere
    could take over any imported judge by inviting their address. The usual
    case, a returning judge invited by the team that imported last year's
    event, passes.
    """
    user = user_by_email(conn, inv["email"])
    if user is None:
        return True
    if user["password_hash"]:
        return False
    return _vouches_for(conn, inv["created_by"], user["id"], besides=inv["event_id"])


CLAIM_DAYS = 14


def unclaimed_accounts(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[sqlite3.Row]:
    """This event's judges and team members whose accounts have no password yet."""
    require_organizer(conn, event_id, actor)
    return conn.execute(
        "SELECT u.id, u.name, u.email, 'judge' AS role FROM event_roles r JOIN users u ON u.id = r.user_id "
        "WHERE r.event_id = ? AND r.role = 'judge' AND u.password_hash IS NULL "
        "UNION SELECT u.id, u.name, u.email, 'team member' AS role FROM team_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.event_id = ? AND u.password_hash IS NULL ORDER BY 3", (event_id, event_id)).fetchall()


def issue_claim_link(conn: sqlite3.Connection, actor: Actor | None, event_id: str, user_id: str) -> str:
    """A single-use link for the owner of an imported account to set its
    password. Returns the raw token; Plumb sends no mail, the organizer does."""
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        user = get_user(conn, user_id)
        if user is None or event_id not in events_of(conn, user_id):
            raise NotFound("no such account in this event")
        if user["password_hash"]:
            raise Conflict("that account already has a password; its owner can log in")
        if not _vouches_for(conn, actor.id, user_id):
            raise Forbidden("that account also belongs to an event you do not organize; ask an organizer of all of "
                            "its events, or an admin, to make the link")
        token = new_token(24)
        conn.execute("INSERT INTO claim_links(token_hash, user_id, created_by, created_at) VALUES (?,?,?,?)",
                     (token_hash(token), user_id, actor.id, now()))
        audit.append(conn, "account.claim_link", actor_id=actor.id, event_id=event_id, subject=user_id, ip=actor.ip)
    return token


def get_claim_link(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT c.*, u.email, u.password_hash FROM claim_links c JOIN users u ON u.id = c.user_id WHERE c.token_hash = ?",
        (token_hash(token),)).fetchone()
    if row is None:
        raise NotFound("that claim link is not valid")
    if row["used_at"] or row["password_hash"]:
        raise Conflict("this account has been claimed already; log in instead")
    if parse_ts(row["created_at"]) < _utcnow() - timedelta(days=CLAIM_DAYS):
        raise Conflict(f"this claim link expired after {CLAIM_DAYS} days; ask the organizer for a new one")
    return row


def use_claim_link(conn: sqlite3.Connection, token: str, name: str, password_hash: str, ip: str | None) -> str:
    with transaction(conn):
        link = get_claim_link(conn, token)
        name = clean_text(name, "name", required=True, max_len=100)
        conn.execute("UPDATE users SET name = ?, password_hash = ? WHERE id = ? AND password_hash IS NULL",
                     (name, password_hash, link["user_id"]))
        conn.execute("UPDATE claim_links SET used_at = ? WHERE token_hash = ?", (now(), token_hash(token)))
        audit.append(conn, "user.claimed", actor_id=link["user_id"], subject=link["user_id"],
                     detail={"via": "claim_link", "issued_by": link["created_by"]}, ip=ip)
    return link["user_id"]


def revoke_invitation(conn: sqlite3.Connection, actor: Actor | None, event_id: str, email: str, role: str) -> int:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        n = conn.execute("DELETE FROM invitations WHERE event_id = ? AND email = ? AND role = ? AND accepted_at IS NULL",
                         (event_id, email, role)).rowcount
        if not n:
            raise NotFound("no pending invitation for that address")
        audit.append(conn, "invitation.revoked", actor_id=actor.id, event_id=event_id, subject=email,
                     detail={"role": role, "links": n}, ip=actor.ip)
    return n


def remove_judge(conn: sqlite3.Connection, actor: Actor | None, event_id: str, judge_id: str) -> None:
    """Take a judge off an event, with their open assignments. A judge who has
    reviewed anything stays: the reviews are on the record, and removing the
    judge would orphan them."""
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM event_roles WHERE event_id = ? AND user_id = ? AND role = 'judge'",
                            (event_id, judge_id)).fetchone():
            raise NotFound("no such judge in this event")
        if conn.execute("SELECT 1 FROM reviews WHERE event_id = ? AND judge_id = ? UNION "
                        "SELECT 1 FROM comparisons WHERE event_id = ? AND judge_id = ?",
                        (event_id, judge_id, event_id, judge_id)).fetchone():
            raise Conflict("this judge has reviews on the record; unassign their open projects instead")
        dropped = conn.execute("DELETE FROM assignments WHERE event_id = ? AND judge_id = ?", (event_id, judge_id)).rowcount
        conn.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event_id, judge_id))
        conn.execute("DELETE FROM event_roles WHERE event_id = ? AND user_id = ? AND role = 'judge'", (event_id, judge_id))
        audit.append(conn, "judge.removed", actor_id=actor.id, event_id=event_id, subject=judge_id,
                     detail={"assignments_dropped": dropped}, ip=actor.ip)


def accept_invitation(conn: sqlite3.Connection, actor: Actor | None, token: str) -> str:
    actor = require_user(actor)
    with transaction(conn):
        inv = get_invitation(conn, token)
        if inv["accepted_at"]:
            raise Conflict("this invitation has already been used")
        if inv["email"].lower() != actor.email.lower():
            raise Forbidden(f"this invitation is for {inv['email']}; log in with that account to accept it")
        if team_for(conn, inv["event_id"], actor.id):
            raise Forbidden(f"you are on a team in this event, so you cannot be its {inv['role']}")
        conn.execute(
            "INSERT OR IGNORE INTO event_roles(event_id, user_id, role) VALUES (?,?,?)",
            (inv["event_id"], actor.id, inv["role"]),
        )
        conn.execute(
            "UPDATE invitations SET accepted_at = ?, accepted_by = ? WHERE token_hash = ?",
            (now(), actor.id, token_hash(token)),
        )
        audit.append(conn, "invitation.accepted", actor_id=actor.id, event_id=inv["event_id"], subject=actor.id,
                     detail={"role": inv["role"]}, ip=actor.ip)
    return inv["event_id"]


def pending_invitations(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT email, role, created_at FROM invitations WHERE event_id = ? AND accepted_at IS NULL ORDER BY created_at DESC",
        (event_id,),
    ).fetchall()


def judges(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, "
        " (SELECT COUNT(*) FROM assignments a WHERE a.judge_id = u.id AND a.event_id = r.event_id) AS assigned, "
        " (SELECT COUNT(*) FROM reviews v WHERE v.judge_id = u.id AND v.event_id = r.event_id) AS reviewed "
        "FROM event_roles r JOIN users u ON u.id = r.user_id WHERE r.event_id = ? AND r.role = 'judge' ORDER BY u.name",
        (event_id,),
    ).fetchall()
    tracks_by_judge: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT jt.user_id, t.id, t.name FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id WHERE jt.event_id = ?",
        (event_id,),
    ):
        tracks_by_judge.setdefault(r["user_id"], []).append(r["id"])
    return [{**dict(r), "tracks": tracks_by_judge.get(r["id"], [])} for r in rows]


def set_judge_tracks(conn: sqlite3.Connection, actor: Actor | None, event_id: str, judge_id: str, track_ids: list[str]) -> None:
    actor = require_organizer(conn, event_id, actor)
    if not conn.execute(
        "SELECT 1 FROM event_roles WHERE event_id = ? AND user_id = ? AND role = 'judge'", (event_id, judge_id)
    ).fetchone():
        raise NotFound("no such judge in this event")
    valid = {t["id"] for t in tracks(conn, event_id)}
    if any(t not in valid for t in track_ids):
        raise Invalid("unknown track")
    with transaction(conn):
        conn.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event_id, judge_id))
        for t in sorted(set(track_ids)):
            conn.execute("INSERT INTO judge_tracks(event_id, user_id, track_id) VALUES (?,?,?)", (event_id, judge_id, t))
        audit.append(conn, "judge.tracks_set", actor_id=actor.id, event_id=event_id, subject=judge_id,
                     detail={"tracks": sorted(set(track_ids))}, ip=actor.ip)


def _assignment_inputs(conn: sqlite3.Connection, event_id: str):
    js = [
        assign.JudgeInfo(id=j["id"], email=j["email"], tracks=frozenset(j["tracks"]))
        for j in judges(conn, event_id)
    ]
    members: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT m.team_id, u.email FROM team_members m JOIN users u ON u.id = m.user_id WHERE m.event_id = ?", (event_id,)
    ):
        members.setdefault(r["team_id"], set()).add(r["email"])
    ps = [
        assign.ProjectInfo(id=p["id"], track=p["track_id"], member_emails=frozenset(members.get(p["team_id"], set())))
        for p in conn.execute(
            f"SELECT p.id, p.track_id, p.team_id FROM projects p WHERE p.event_id = ? AND {live_entry('p')}", (event_id,)
        )
    ]
    existing = {(r["judge_id"], r["project_id"]) for r in conn.execute(
        "SELECT judge_id, project_id FROM assignments WHERE event_id = ?", (event_id,)
    )}
    return js, ps, existing


def auto_assign(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        event = get_event(conn, event_id)
        if event["results_published_at"]:
            raise Conflict("results are published; assignments are locked")
        js, ps, existing = _assignment_inputs(conn, event_id)
        new, short = assign.propose(js, ps, existing, event["reviews_per_project"])
        ts = now()
        for j, p in new:
            conn.execute(
                "INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) VALUES (?,?,?, 'auto', ?)",
                (event_id, j, p, ts),
            )
        audit.append(conn, "assignments.auto", actor_id=actor.id, event_id=event_id, subject=event_id,
                     detail={"added": len(new), "target": event["reviews_per_project"], "short": short}, ip=actor.ip)
    return {"added": len(new), "short": short}


def assign_manual(conn: sqlite3.Connection, actor: Actor | None, event_id: str, judge_id: str, project_id: str) -> None:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        js, ps, existing = _assignment_inputs(conn, event_id)
        judge = next((j for j in js if j.id == judge_id), None)
        project = next((p for p in ps if p.id == project_id), None)
        if judge is None or project is None:
            raise Invalid("pick a judge and a submitted project from this event")
        if (judge_id, project_id) in existing:
            raise Conflict("already assigned")
        why = assign.conflict(judge, project)
        if why:
            raise Conflict(f"conflict of interest: {why}")
        conn.execute(
            "INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) VALUES (?,?,?, 'manual', ?)",
            (event_id, judge_id, project_id, now()),
        )
        audit.append(conn, "assignment.added", actor_id=actor.id, event_id=event_id, subject=project_id,
                     detail={"judge": judge_id}, ip=actor.ip)


def unassign(conn: sqlite3.Connection, actor: Actor | None, event_id: str, judge_id: str, project_id: str) -> None:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM reviews WHERE event_id = ? AND judge_id = ? AND project_id = ?",
                        (event_id, judge_id, project_id)).fetchone():
            raise Conflict("this judge has already reviewed the project; the review stays on the record")
        cur = conn.execute(
            "DELETE FROM assignments WHERE event_id = ? AND judge_id = ? AND project_id = ?", (event_id, judge_id, project_id)
        )
        if cur.rowcount == 0:
            raise NotFound("no such assignment")
        audit.append(conn, "assignment.removed", actor_id=actor.id, event_id=event_id, subject=project_id,
                     detail={"judge": judge_id}, ip=actor.ip)


def release_open_assignments(conn: sqlite3.Connection, actor: Actor | None, event_id: str, judge_id: str) -> int:
    """Take back every project a judge was given but has not reviewed, so
    automatic assignment can give them to someone else. Auto-assign counts
    assignments, not reviews, so a judge who stopped reviewing holds their
    projects until this runs. Their reviews stay."""
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        released = [r[0] for r in conn.execute(
            "SELECT a.project_id FROM assignments a WHERE a.event_id = ? AND a.judge_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM reviews v WHERE v.event_id = a.event_id AND v.judge_id = a.judge_id AND v.project_id = a.project_id)",
            (event_id, judge_id))]
        if not released:
            raise NotFound("this judge has no open assignments")
        conn.executemany("DELETE FROM assignments WHERE event_id = ? AND judge_id = ? AND project_id = ?",
                         [(event_id, judge_id, p) for p in released])
        audit.append(conn, "assignments.released", actor_id=actor.id, event_id=event_id, subject=judge_id,
                     detail={"projects": released}, ip=actor.ip)
    return len(released)


def judge_queue(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[sqlite3.Row]:
    actor = require_judge(conn, event_id, actor)
    return conn.execute(
        f"SELECT {PROJECT_COLUMNS}, v.id AS review_id, v.updated_at AS reviewed_at, a.source "
        f"{PROJECT_FROM} JOIN assignments a ON a.project_id = p.id "
        "LEFT JOIN reviews v ON v.project_id = p.id AND v.judge_id = a.judge_id "
        f"WHERE a.event_id = ? AND a.judge_id = ? AND {live_entry('p')} ORDER BY (v.id IS NOT NULL), p.title",
        (event_id, actor.id),
    ).fetchall()


def own_review(conn: sqlite3.Connection, actor: Actor, project_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM reviews WHERE judge_id = ? AND project_id = ?", (actor.id, project_id)).fetchone()
    if row is None:
        return None
    values = {r["key"]: r["value"] for r in conn.execute(
        "SELECT c.key, s.value FROM review_scores s JOIN criteria c ON c.id = s.criterion_id WHERE s.review_id = ?",
        (row["id"],),
    )}
    return {**dict(row), "values": values}


def save_review(conn: sqlite3.Connection, actor: Actor | None, project_id: str, values: dict, comment: str) -> str:
    actor = require_user(actor)
    with transaction(conn):
        project = get_project(conn, project_id)
        event_id = project["event_id"]
        if "judge" not in roles(conn, event_id, actor):
            raise Forbidden("only this event's judges can review")
        if not conn.execute(
            "SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?", (actor.id, project_id)
        ).fetchone():
            raise Forbidden("this project is not assigned to you")
        if project["status"] != "submitted" or project["duplicate_of"] or project["disqualified_reason"]:
            raise Conflict("this entry is not in judging (a draft, a duplicate or disqualified)")
        event = get_event(conn, event_id)
        if not phase(event)["judging_open"]:
            if event["results_published_at"]:
                raise Conflict("results are published; reviews are locked")
            raise Conflict("judging is not open for this event")
        crit = criteria(conn, event_id)
        clean: dict[str, int] = {}
        for c in crit:
            raw = values.get(c["key"])
            try:
                v = int(raw)
            except (TypeError, ValueError) as exc:
                raise Invalid(f"{c['name']} needs a score") from exc
            if not c["min_value"] <= v <= c["max_value"]:
                raise Invalid(f"{c['name']} must be between {c['min_value']} and {c['max_value']}")
            clean[c["key"]] = v
        comment = clean_text(comment, "comment", max_len=4000)
        ts = now()
        existing = conn.execute(
            "SELECT id FROM reviews WHERE judge_id = ? AND project_id = ?", (actor.id, project_id)
        ).fetchone()
        if existing:
            rid = existing["id"]
            before = own_review(conn, actor, project_id)
            conn.execute("UPDATE reviews SET comment = ?, updated_at = ? WHERE id = ?", (comment, ts, rid))
            conn.execute("DELETE FROM review_scores WHERE review_id = ?", (rid,))
            action, detail = "review.edited", {"from": before["values"], "to": clean}
        else:
            rid = new_id("rev")
            conn.execute(
                "INSERT INTO reviews(id, event_id, judge_id, project_id, comment, submitted_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (rid, event_id, actor.id, project_id, comment, ts, ts),
            )
            action, detail = "review.submitted", {"values": clean}
        for c in crit:
            conn.execute(
                "INSERT INTO review_scores(review_id, criterion_id, value) VALUES (?,?,?)", (rid, c["id"], clean[c["key"]])
            )
        audit.append(conn, action, actor_id=actor.id, event_id=event_id, subject=project_id, detail=detail, ip=actor.ip)
    return rid


def reviews_for(conn: sqlite3.Connection, event_id: str | None = None, judge_id: str | None = None) -> list[dict]:
    """Every review with its criterion values. No permission check: callers
    must have decided the viewer may see these rows."""
    where, args = [], []
    if event_id:
        where.append("v.event_id = ?")
        args.append(event_id)
    if judge_id:
        where.append("v.judge_id = ?")
        args.append(judge_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT v.*, u.name AS judge_name, u.email AS judge_email, p.title AS project_title, p.team_id, "
        " p.track_id, p.duplicate_of, p.disqualified_reason, p.status AS project_status "
        f"FROM reviews v JOIN users u ON u.id = v.judge_id JOIN projects p ON p.id = v.project_id {clause} "
        "ORDER BY v.event_id, p.id, u.id",
        args,
    ).fetchall()
    values: dict[str, dict[str, int]] = {}
    ids = [r["id"] for r in rows]
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for s in conn.execute(
            f"SELECT s.review_id, c.key, s.value FROM review_scores s JOIN criteria c ON c.id = s.criterion_id "
            f"WHERE s.review_id IN ({','.join('?' * len(part))})", part
        ):
            values.setdefault(s["review_id"], {})[s["key"]] = s["value"]
    return [{**dict(r), "values": values.get(r["id"], {})} for r in rows]


def judge_scores(conn: sqlite3.Connection, actor: Actor | None, judge_id: str | None = None,
                 event_id: str | None = None) -> list[dict]:
    """A judge's reviews.

    A judge may read their own. Reading anyone else's needs organizer rights
    on every event those reviews belong to; otherwise the whole request is
    refused, not filtered, so a probe cannot tell an empty list from a
    forbidden one.
    """
    actor = require_user(actor)
    target = judge_id or actor.id
    if target == actor.id:
        judged = conn.execute(
            "SELECT 1 FROM event_roles WHERE user_id = ? AND role = 'judge'" + (" AND event_id = ?" if event_id else ""),
            (actor.id, event_id) if event_id else (actor.id,),
        ).fetchone()
        if not judged:
            raise Forbidden("only judges have scores to read")
        return reviews_for(conn, event_id=event_id, judge_id=actor.id)
    events_of_target = [r["event_id"] for r in conn.execute(
        "SELECT event_id FROM event_roles WHERE user_id = ? AND role = 'judge'" + (" AND event_id = ?" if event_id else ""),
        (target, event_id) if event_id else (target,),
    )]
    if not events_of_target or not all(is_organizer(conn, e, actor) for e in events_of_target):
        raise Forbidden("judges can only read their own scores")
    return reviews_for(conn, event_id=event_id, judge_id=target)


def reviews_set_aside(conn: sqlite3.Connection, event_id: str) -> int:
    """Reviews that score an entry that does not count (a dropped duplicate, a
    disqualified entry). They stay in the database and exports, not in the ranking."""
    return conn.execute(
        "SELECT COUNT(*) FROM reviews v JOIN projects p ON p.id = v.project_id WHERE p.event_id = ? "
        "AND (p.duplicate_of IS NOT NULL OR p.disqualified_reason IS NOT NULL)",
        (event_id,),
    ).fetchone()[0]


def progress(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    require_organizer(conn, event_id, actor)
    event = get_event(conn, event_id)
    target = event["reviews_per_project"]
    rows = conn.execute(
        "SELECT p.id, p.title, p.track_id, t.name AS team_name, "
        " (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) AS assigned, "
        " (SELECT COUNT(*) FROM reviews v WHERE v.project_id = p.id) AS reviewed "
        f"FROM projects p JOIN teams t ON t.id = p.team_id WHERE p.event_id = ? AND {live_entry('p')} "
        "ORDER BY reviewed, p.id",
        (event_id,),
    ).fetchall()
    js = judges(conn, event_id)
    total_assigned = sum(r["assigned"] for r in rows)
    total_reviewed = sum(r["reviewed"] for r in rows)
    return {
        "target": target,
        "projects": rows,
        "judges": js,
        "assigned": total_assigned,
        "reviewed": total_reviewed,
        "under_target": sum(1 for r in rows if r["reviewed"] < target),
        "unassigned_projects": sum(1 for r in rows if r["assigned"] == 0),
        "idle_judges": [j for j in js if j["assigned"] > j["reviewed"]],
        "set_aside": reviews_set_aside(conn, event_id),
    }
