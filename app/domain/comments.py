"""Comments and moderation."""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import new_id, now, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Conflict,
    NotFound,
    clean_text,
)
from .events import (  # noqa: F401
    is_organizer,
    require_organizer,
    require_user,
)
from .projects import (  # noqa: F401
    get_project,
    view_project,
)


def comments(conn: sqlite3.Connection, actor: Actor | None, project_id: str) -> list[sqlite3.Row]:
    project = get_project(conn, project_id)
    show_hidden = is_organizer(conn, project["event_id"], actor)
    return conn.execute(
        "SELECT c.*, u.name AS author FROM comments c JOIN users u ON u.id = c.user_id WHERE c.project_id = ? "
        + ("" if show_hidden else "AND c.hidden_at IS NULL ") + "ORDER BY c.created_at",
        (project_id,),
    ).fetchall()


def add_comment(conn: sqlite3.Connection, actor: Actor | None, project_id: str, body: str) -> str:
    actor = require_user(actor)
    body = clean_text(body, "comment", required=True, max_len=2000)
    with transaction(conn):
        project = view_project(conn, actor, project_id)
        if project["status"] != "submitted":
            raise Conflict("comments open once the project is submitted")
        cid = new_id("cmt")
        conn.execute(
            "INSERT INTO comments(id, project_id, user_id, body, created_at) VALUES (?,?,?,?,?)",
            (cid, project_id, actor.id, body, now()),
        )
        audit.append(conn, "comment.added", actor_id=actor.id, event_id=project["event_id"], subject=cid,
                     detail={"project": project_id, "length": len(body)}, ip=actor.ip)
    return cid


def hide_comment(conn: sqlite3.Connection, actor: Actor | None, comment_id: str, reason: str) -> None:
    row = conn.execute(
        "SELECT c.*, p.event_id FROM comments c JOIN projects p ON p.id = c.project_id WHERE c.id = ?", (comment_id,)
    ).fetchone()
    if row is None:
        raise NotFound("no such comment")
    actor = require_organizer(conn, row["event_id"], actor)
    reason = clean_text(reason, "reason", max_len=300) or "hidden by an organizer"
    with transaction(conn):
        conn.execute(
            "UPDATE comments SET hidden_at = ?, hidden_by = ?, hidden_reason = ? WHERE id = ?",
            (now(), actor.id, reason, comment_id),
        )
        audit.append(conn, "comment.hidden", actor_id=actor.id, event_id=row["event_id"], subject=comment_id,
                     detail={"reason": reason}, ip=actor.ip)
