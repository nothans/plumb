"""The community vote and its abuse signals.

Secrecy: while the vote is open, the audit log holds each vote only as a
seal, sha256(nonce:project), with no voter and no address, so neither the
log, its webhook stream, nor an organizer can say who voted for what. At the
close the nonces are revealed (still without voters), and anyone can check
every counted vote against the sealed log. The link from a voter to their
votes lives only in the votes table, for abuse review, and no page or
endpoint shows it together with the projects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .. import audit
from ..db import now, transaction
from ..security import new_token
from .core import (
    Actor,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    clean_text,
)
from .events import (
    get_event,
    phase,
    require_organizer,
    require_user,
    roles,
)
from .projects import (
    PROJECT_COLUMNS,
    PROJECT_FROM,
    gallery,
    get_project,
    is_live,
    live_entry,
)
from .teams import (
    team_for,
)


def ballot_order_key(voter_id: str, event_id: str, project_id: str) -> str:
    return hashlib.sha256(f"{event_id}|{voter_id}|{project_id}".encode()).hexdigest()


def _voter_check(conn: sqlite3.Connection, event: sqlite3.Row, actor: Actor) -> None:
    held = roles(conn, event["id"], actor)
    if "judge" in held:
        raise Forbidden("judges do not vote in the community ballot")
    if held & {"organizer", "admin"}:
        raise Forbidden("organizers and admins run the vote, so they do not vote in it")
    if event["voting_mode"] == "participants":
        # A participant is someone whose team has a live entry, not anyone
        # who made an empty team to get a ballot.
        team = team_for(conn, event["id"], actor.id)
        if team is None or not conn.execute(
            f"SELECT 1 FROM projects p WHERE p.team_id = ? AND {live_entry('p')}", (team["id"],)
        ).fetchone():
            raise Forbidden("only members of a team with a submitted entry can vote")


def vote_refusal(conn: sqlite3.Connection, event: sqlite3.Row, actor: Actor | None) -> str | None:
    """Why this visitor cannot vote in this event, or None if they can, so a
    page never offers a ballot that will be refused."""
    if actor is None:
        return "log in to vote"
    try:
        _voter_check(conn, event, actor)
    except Forbidden as exc:
        return exc.message
    return None


def ballot(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """The voter's ballot, in an order unique to them.

    Each voter sees the projects shuffled by a hash of (event, voter,
    project): stable across reloads, different between voters, and not
    something a voter can steer.
    """
    actor = require_user(actor)
    event = get_event(conn, event_id)
    if phase(event)["voting"] != "open":
        raise Conflict("voting is not open")
    _voter_check(conn, event, actor)
    rows, _ = gallery(conn, event_id=event_id, limit=10000)
    rows.sort(key=lambda r: ballot_order_key(actor.id, event_id, r["id"]))
    mine = {r["project_id"] for r in conn.execute(
        "SELECT project_id FROM votes WHERE event_id = ? AND voter_id = ?", (event_id, actor.id)
    )}
    own_team = team_for(conn, event_id, actor.id)
    return {"projects": rows, "votes": mine, "remaining": event["votes_per_voter"] - len(mine),
            "own_team": own_team["id"] if own_team else None, "event": event}


def cast_vote(conn: sqlite3.Connection, actor: Actor | None, project_id: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        project = get_project(conn, project_id)
        event = get_event(conn, project["event_id"])
        if phase(event)["voting"] != "open":
            raise Conflict("voting is not open")
        _voter_check(conn, event, actor)
        if not is_live(project):
            raise Invalid("this project is not on the ballot")
        if conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (project["team_id"], actor.id)).fetchone():
            raise Forbidden("you cannot vote for your own team")
        used = conn.execute("SELECT COUNT(*) FROM votes WHERE event_id = ? AND voter_id = ?", (event["id"], actor.id)).fetchone()[0]
        if conn.execute("SELECT 1 FROM votes WHERE voter_id = ? AND project_id = ?", (actor.id, project_id)).fetchone():
            raise Conflict("you already voted for this project")
        if used >= event["votes_per_voter"]:
            raise Conflict(f"you have used all {event['votes_per_voter']} of your votes")
        nonce = new_token(16)
        conn.execute(
            "INSERT INTO votes(event_id, voter_id, project_id, created_at, ip, nonce) VALUES (?,?,?,?,?,?)",
            (event["id"], actor.id, project_id, now(), actor.ip, nonce),
        )
        # Sealed and anonymous: the log proves a vote was cast and pins what
        # it was, without saying what it was until the nonces are revealed at
        # close, and without ever saying whose it was.
        audit.append(conn, "vote.cast", actor_id=None, event_id=event["id"], subject="sealed",
                     detail={"seal": vote_seal(nonce, project_id)})


def vote_seal(nonce: str, project_id: str) -> str:
    return hashlib.sha256(f"{nonce}:{project_id}".encode()).hexdigest()


def vote_reveal(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """After voting closes: every counted vote as (seal, project, nonce),
    without voters. Anyone can recompute each seal, find it among the
    audit log's vote.cast entries, and so check the tally against history."""
    event = get_event(conn, event_id)
    if phase(event)["voting"] != "closed":
        raise Forbidden("votes are sealed until voting closes")
    votes = [{"seal": vote_seal(r["nonce"] or "", r["project_id"]), "project": r["project_id"], "nonce": r["nonce"]}
             for r in conn.execute("SELECT project_id, nonce FROM votes WHERE event_id = ? ORDER BY nonce", (event_id,))]
    return {"event": event_id, "votes": votes}


def withdraw_vote(conn: sqlite3.Connection, actor: Actor | None, project_id: str) -> None:
    actor = require_user(actor)
    with transaction(conn):
        project = get_project(conn, project_id)
        event = get_event(conn, project["event_id"])
        if phase(event)["voting"] != "open":
            raise Conflict("voting is not open")
        row = conn.execute("SELECT nonce FROM votes WHERE voter_id = ? AND project_id = ?", (actor.id, project_id)).fetchone()
        if row is None:
            raise NotFound("you have not voted for this project")
        conn.execute("DELETE FROM votes WHERE voter_id = ? AND project_id = ?", (actor.id, project_id))
        audit.append(conn, "vote.withdrawn", actor_id=None, event_id=event["id"], subject="sealed",
                     detail={"seal": vote_seal(row["nonce"] or "", project_id)})


def vote_tallies(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[sqlite3.Row]:
    """Hidden from everyone, organizers included, until voting closes.
    Nobody can nudge a close race they can see."""
    event = get_event(conn, event_id)
    if phase(event)["voting"] != "closed":
        raise Forbidden("vote counts stay hidden until voting closes")
    return conn.execute(
        f"SELECT {PROJECT_COLUMNS}, COUNT(v.voter_id) AS votes {PROJECT_FROM} "
        "LEFT JOIN votes v ON v.project_id = p.id "
        f"WHERE p.event_id = ? AND {live_entry('p')} "
        "GROUP BY p.id ORDER BY votes DESC, p.id",
        (event_id,),
    ).fetchall()


def turnout(conn: sqlite3.Connection, event_id: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS votes, COUNT(DISTINCT voter_id) AS voters FROM votes WHERE event_id = ?", (event_id,)
    ).fetchone()
    return dict(row)


def abuse_signals(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    """Patterns worth a human look. None of these block anything on their own."""
    require_organizer(conn, event_id, actor)
    event = get_event(conn, event_id)
    shared_ip = conn.execute(
        "SELECT v.ip, COUNT(DISTINCT v.voter_id) AS voters, GROUP_CONCAT(DISTINCT u.email) AS voter_ids "
        "FROM votes v JOIN users u ON u.id = v.voter_id WHERE v.event_id = ? AND v.ip IS NOT NULL "
        "GROUP BY v.ip HAVING voters >= 3 ORDER BY voters DESC",
        (event_id,),
    ).fetchall()
    fresh = []
    if event["voting_open"]:
        fresh = conn.execute(
            "SELECT u.id, u.email, u.created_at, COUNT(v.project_id) AS votes FROM votes v JOIN users u ON u.id = v.voter_id "
            "WHERE v.event_id = ? AND u.created_at >= ? GROUP BY u.id ORDER BY u.created_at",
            (event_id, event["voting_open"]),
        ).fetchall()
    concentrated = conn.execute(
        "SELECT v.project_id, p.title, COUNT(*) AS votes, "
        " SUM(CASE WHEN u.created_at >= COALESCE(?, '9999') THEN 1 ELSE 0 END) AS from_new_accounts "
        "FROM votes v JOIN users u ON u.id = v.voter_id JOIN projects p ON p.id = v.project_id "
        "WHERE v.event_id = ? GROUP BY v.project_id HAVING from_new_accounts >= 3 ORDER BY from_new_accounts DESC",
        (event["voting_open"], event_id),
    ).fetchall()
    if phase(event)["voting"] == "open":
        # Naming the project, or its total, would leak the race to organizers.
        concentrated = [{"project_id": None, "title": "one project (named when voting closes)", "votes": None,
                         "from_new_accounts": r["from_new_accounts"]} for r in concentrated]
    hidden_comments = conn.execute(
        "SELECT COUNT(*) FROM comments c JOIN projects p ON p.id = c.project_id WHERE p.event_id = ? AND c.hidden_at IS NOT NULL",
        (event_id,),
    ).fetchone()[0]
    return {"shared_ip": shared_ip, "fresh_accounts": fresh, "concentrated": concentrated,
            "hidden_comments": hidden_comments}


def remove_votes_of(conn: sqlite3.Connection, actor: Actor | None, event_id: str, voter_id: str, reason: str) -> int:
    """Void every vote of one account, with a reason. The seals of the voided
    votes go into the log, so the public tally check can account for them.
    Refused once results are published: the signed record carries the vote."""
    actor = require_organizer(conn, event_id, actor)
    reason = clean_text(reason, "reason", required=True, max_len=300)
    with transaction(conn):
        if get_event(conn, event_id)["results_published_at"]:
            raise Conflict("results are published and the signed record carries the vote; it can no longer change")
        rows = conn.execute("SELECT project_id, nonce FROM votes WHERE event_id = ? AND voter_id = ?",
                            (event_id, voter_id)).fetchall()
        if not rows:
            raise NotFound("that account has no votes in this event")
        conn.execute("DELETE FROM votes WHERE event_id = ? AND voter_id = ?", (event_id, voter_id))
        seals = sorted(vote_seal(r["nonce"] or "", r["project_id"]) for r in rows)
        audit.append(conn, "votes.voided", actor_id=actor.id, event_id=event_id, subject=voter_id,
                     detail={"removed": len(rows), "reason": reason, "seals": seals}, ip=actor.ip)
    return len(rows)


def vote_log(conn: sqlite3.Connection, event_id: str) -> dict:
    """After voting closes, public: every vote entry of the audit log, with
    no voters, and the revealed votes, so anyone can recompute the tally:
    counted = cast seals - withdrawn seals - voided seals, and each revealed
    vote's sha256(nonce:project) must be one of the counted seals."""
    event = get_event(conn, event_id)
    if phase(event)["voting"] != "closed":
        raise Forbidden("votes are sealed until voting closes")
    entries = []
    for r in conn.execute(
        "SELECT seq, at, action, detail FROM audit_log WHERE event_id = ? "
        "AND action IN ('vote.cast', 'vote.withdrawn', 'votes.voided') ORDER BY seq", (event_id,)
    ):
        detail = json.loads(r["detail"])
        if r["action"] == "votes.voided":
            detail = {"removed": detail.get("removed"), "reason": detail.get("reason"), "seals": detail.get("seals", [])}
        entries.append({"seq": r["seq"], "at": r["at"], "action": r["action"], "detail": detail})
    return {"event": event_id, "entries": entries, "votes": vote_reveal(conn, None, event_id)["votes"]}


def counted_seals(log: dict) -> set[str]:
    """The tally rule, as a checker would apply it to vote_log's output."""
    cast, gone = set(), set()
    for e in log["entries"]:
        if e["action"] == "vote.cast":
            cast.add(e["detail"]["seal"])
        elif e["action"] == "vote.withdrawn":
            gone.add(e["detail"]["seal"])
        else:
            gone.update(e["detail"].get("seals", []))
    return cast - gone


def tally_summary(conn: sqlite3.Connection, event_id: str) -> dict | None:
    """The closed community vote, as the results record signs it."""
    event = get_event(conn, event_id)
    if event["voting_mode"] == "off" or phase(event)["voting"] != "closed":
        return None
    seals = sorted(v["seal"] for v in vote_reveal(conn, None, event_id)["votes"])
    voided = [json.loads(r["detail"]) for r in conn.execute(
        "SELECT detail FROM audit_log WHERE event_id = ? AND action = 'votes.voided' ORDER BY seq", (event_id,))]
    return {
        "mode": event["voting_mode"], "opened": event["voting_open"], "closed": event["voting_close"],
        **turnout(conn, event_id),
        "tally": [{"project": r["id"], "votes": r["votes"]} for r in vote_tallies(conn, None, event_id)],
        "counted_seals_sha256": hashlib.sha256(" ".join(seals).encode()).hexdigest(),
        "voided": {"votes": sum(v.get("removed", 0) for v in voided), "reasons": [v.get("reason") for v in voided]},
    }
