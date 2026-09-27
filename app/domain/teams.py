"""Teams and invite links."""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import new_id, now, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Conflict,
    Forbidden,
    NotFound,
    clean_text,
)
from .events import (  # noqa: F401
    get_event,
    phase,
    require_user,
)


def team_for(conn: sqlite3.Connection, event_id: str, user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id WHERE t.event_id = ? AND m.user_id = ?",
        (event_id, user_id),
    ).fetchone()


def team_members(conn: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT u.id, u.name, u.email, m.joined_at FROM team_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.team_id = ? ORDER BY m.joined_at", (team_id,)
    ).fetchall()


def _can_join(conn: sqlite3.Connection, event: sqlite3.Row, actor: Actor) -> None:
    if phase(event)["submissions"] == "closed":
        raise Conflict("this event has closed; teams can no longer change")
    if team_for(conn, event["id"], actor.id):
        raise Conflict("you are already on a team for this event")
    if conn.execute(
        "SELECT 1 FROM event_roles WHERE event_id = ? AND user_id = ?", (event["id"], actor.id)
    ).fetchone():
        raise Forbidden("judges and organizers of an event cannot also compete in it")


def create_team(conn: sqlite3.Connection, actor: Actor | None, event_id: str, name: str) -> str:
    actor = require_user(actor)
    name = clean_text(name, "team name", required=True, max_len=80)
    with transaction(conn):
        event = get_event(conn, event_id)
        _can_join(conn, event, actor)
        if conn.execute("SELECT 1 FROM teams WHERE event_id = ? AND name = ? COLLATE NOCASE", (event_id, name)).fetchone():
            raise Conflict("a team with that name already exists in this event")
        tid = new_id("tm")
        conn.execute(
            "INSERT INTO teams(id, event_id, name, invite_code, created_at) VALUES (?,?,?,?,?)",
            (tid, event_id, name, new_token(12), now()),
        )
        conn.execute(
            "INSERT INTO team_members(team_id, event_id, user_id, joined_at) VALUES (?,?,?,?)",
            (tid, event_id, actor.id, now()),
        )
        audit.append(conn, "team.created", actor_id=actor.id, event_id=event_id, subject=tid,
                     detail={"name": name}, ip=actor.ip)
    return tid


def team_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM teams WHERE invite_code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound("that invite link is not valid (it may have been reset)")
    return row


def join_team(conn: sqlite3.Connection, actor: Actor | None, code: str) -> str:
    actor = require_user(actor)
    with transaction(conn):
        team = team_by_code(conn, code)
        event = get_event(conn, team["event_id"])
        _can_join(conn, event, actor)
        size = conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],)).fetchone()[0]
        if size >= event["max_team_size"]:
            raise Conflict(f"this team is full (teams are at most {event['max_team_size']})")
        conn.execute(
            "INSERT INTO team_members(team_id, event_id, user_id, joined_at) VALUES (?,?,?,?)",
            (team["id"], event["id"], actor.id, now()),
        )
        audit.append(conn, "team.joined", actor_id=actor.id, event_id=event["id"], subject=team["id"], ip=actor.ip)
    return team["id"]


def leave_team(conn: sqlite3.Connection, actor: Actor | None, team_id: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team is None:
            raise NotFound("no such team")
        event = get_event(conn, team["event_id"])
        if phase(event)["submissions"] == "closed":
            raise Conflict("this event has closed; teams can no longer change")
        if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, actor.id)).fetchone():
            raise Forbidden("you are not on this team")
        members = conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)).fetchone()[0]
        if members == 1 and conn.execute(
            "SELECT 1 FROM projects WHERE team_id = ? AND status = 'submitted'", (team_id,)
        ).fetchone():
            raise Conflict("you are the last member and the team has a submitted project; move it back to draft first")
        conn.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, actor.id))
        left = conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)).fetchone()[0]
        if left == 0:
            conn.execute("DELETE FROM projects WHERE team_id = ? AND status = 'draft'", (team_id,))
            if not conn.execute("SELECT 1 FROM projects WHERE team_id = ?", (team_id,)).fetchone():
                conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        audit.append(conn, "team.left", actor_id=actor.id, event_id=event["id"], subject=team_id,
                     detail={"members_left": left}, ip=actor.ip)


def reset_invite(conn: sqlite3.Connection, actor: Actor | None, team_id: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, actor.id)).fetchone():
            raise Forbidden("you are not on this team")
        conn.execute("UPDATE teams SET invite_code = ? WHERE id = ?", (new_token(12), team_id))
        team = conn.execute("SELECT event_id FROM teams WHERE id = ?", (team_id,)).fetchone()
        audit.append(conn, "team.invite_reset", actor_id=actor.id, event_id=team["event_id"], subject=team_id, ip=actor.ip)
