"""Events, roles, phases, schedule rules, tracks, prizes and the rubric."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from .. import audit
from ..db import new_id, now, parse_ts, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    Unauthorized,
    _as_str,
    _utcnow,
    clean_text,
    clean_ts,
)


def get_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise NotFound("no such event")
    return row


def list_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events ORDER BY submissions_close DESC").fetchall()


def roles(conn: sqlite3.Connection, event_id: str, actor: Actor | None) -> set[str]:
    if actor is None:
        return set()
    out = {r["role"] for r in conn.execute(
        "SELECT role FROM event_roles WHERE event_id = ? AND user_id = ?", (event_id, actor.id)
    )}
    if conn.execute("SELECT 1 FROM team_members WHERE event_id = ? AND user_id = ?", (event_id, actor.id)).fetchone():
        out.add("participant")
    if actor.is_admin:
        out.add("admin")
    return out


def is_organizer(conn: sqlite3.Connection, event_id: str, actor: Actor | None) -> bool:
    return bool(roles(conn, event_id, actor) & {"organizer", "admin"})


def require_user(actor: Actor | None) -> Actor:
    if actor is None:
        raise Unauthorized("log in first")
    return actor


def require_organizer(conn: sqlite3.Connection, event_id: str, actor: Actor | None) -> Actor:
    actor = require_user(actor)
    if not is_organizer(conn, event_id, actor):
        raise Forbidden("only this event's organizers can do that")
    return actor


def require_judge(conn: sqlite3.Connection, event_id: str, actor: Actor | None) -> Actor:
    actor = require_user(actor)
    if "judge" not in roles(conn, event_id, actor):
        raise Forbidden("only this event's judges can do that")
    return actor


def phase(event: sqlite3.Row, at: datetime | None = None) -> dict:
    t = at or _utcnow()
    opens, closes = parse_ts(event["submissions_open"]), parse_ts(event["submissions_close"])
    judging_close = parse_ts(event["judging_close"])
    v_open, v_close = parse_ts(event["voting_open"]), parse_ts(event["voting_close"])
    published = event["results_published_at"] is not None
    if event["voting_mode"] == "off" or v_open is None or v_close is None:
        voting = "off"
    elif t < v_open:
        voting = "upcoming"
    elif t < v_close:
        voting = "open"
    else:
        voting = "closed"
    return {
        "submissions": "upcoming" if t < opens else ("open" if t < closes else "closed"),
        "judging_open": t >= closes and not published and (judging_close is None or t < judging_close),
        "voting": voting,
        "results_published": published,
    }


EVENT_FIELDS = (
    "name", "tagline", "description", "submissions_open", "submissions_close", "judging_close",
    "voting_mode", "voting_open", "voting_close", "votes_per_voter", "reviews_per_project", "max_team_size",
    "pairwise",
)


def _flag_value(value) -> int:
    """A checkbox or JSON boolean. An HTML form sends a hidden 0 and, when
    ticked, a 1 after it, so the last value wins."""
    if isinstance(value, list):
        value = value[-1] if value else 0
    return 1 if str(value).strip().lower() in ("1", "true", "on", "yes") else 0

DEFAULT_CRITERIA = [
    ("functionality", "Functionality", "Does it work, end to end?"),
    ("quality", "Quality", "Is it built well: code, design, docs?"),
    ("innovation", "Innovation", "Is it a new idea or a new take?"),
]


def _event_values(form: dict, *, partial: dict | None = None) -> dict:
    base = dict(partial or {})
    for key in EVENT_FIELDS:
        if key in form and form[key] is not None:
            base[key] = form[key]
    out = {
        "name": clean_text(base.get("name"), "name", required=True, max_len=120),
        "tagline": clean_text(base.get("tagline"), "tagline", max_len=200),
        "description": clean_text(base.get("description"), "description", max_len=5000),
        "submissions_open": clean_ts(base.get("submissions_open"), "submissions open", required=True),
        "submissions_close": clean_ts(base.get("submissions_close"), "submissions close", required=True),
        "judging_close": clean_ts(base.get("judging_close"), "judging close"),
        "voting_mode": (base.get("voting_mode") or "off"),
        "voting_open": clean_ts(base.get("voting_open"), "voting open"),
        "voting_close": clean_ts(base.get("voting_close"), "voting close"),
    }
    if out["voting_mode"] not in ("off", "accounts", "participants"):
        raise Invalid("voting mode must be off, accounts or participants")
    for key, lo, hi in (("votes_per_voter", 1, 50), ("reviews_per_project", 1, 20), ("max_team_size", 1, 50)):
        raw = base.get(key)
        if raw is None or raw == "":
            raw = {"votes_per_voter": 3, "reviews_per_project": 3, "max_team_size": 4}[key]
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise Invalid(f"{key.replace('_', ' ')} must be a whole number") from exc
        if not lo <= value <= hi:
            raise Invalid(f"{key.replace('_', ' ')} must be between {lo} and {hi}")
        out[key] = value
    out["pairwise"] = _flag_value(base.get("pairwise", 0))
    if out["submissions_open"] >= out["submissions_close"]:
        raise Invalid("submissions must open before they close")
    if out["judging_close"] and out["judging_close"] <= out["submissions_close"]:
        raise Invalid("judging must close after submissions close")
    if out["voting_mode"] != "off":
        if not (out["voting_open"] and out["voting_close"]):
            raise Invalid("voting needs an opening and a closing time")
        if out["voting_open"] >= out["voting_close"]:
            raise Invalid("voting must open before it closes")
    return out


def create_event(conn: sqlite3.Connection, actor: Actor | None, form: dict, tracks: list[str], prizes: list[dict],
                 *, event_id: str | None = None) -> str:
    actor = require_user(actor)
    if not actor.is_admin:
        raise Forbidden("only admins can create events")
    values = _event_values(form)
    tracks = [clean_text(t, "track", max_len=80) for t in tracks if t and t.strip()]
    if len(set(t.lower() for t in tracks)) != len(tracks):
        raise Invalid("track names must be different from each other")
    eid = event_id or new_id("evt")
    with transaction(conn):
        conn.execute(
            f"INSERT INTO events(id, {', '.join(values)}, created_at) VALUES (?, {', '.join('?' * len(values))}, ?)",
            (eid, *values.values(), now()),
        )
        for name in tracks:
            conn.execute("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)", (new_id("trk"), eid, name))
        _save_prizes(conn, eid, prizes)
        for pos, (key, name, desc) in enumerate(DEFAULT_CRITERIA):
            conn.execute(
                "INSERT INTO criteria(id, event_id, key, name, description, weight, position) VALUES (?,?,?,?,?,1,?)",
                (new_id("crt"), eid, key, name, desc, pos),
            )
        conn.execute("INSERT INTO event_roles(event_id, user_id, role) VALUES (?,?, 'organizer')", (eid, actor.id))
        audit.append(conn, "event.created", actor_id=actor.id, event_id=eid, subject=eid,
                     detail={**values, "tracks": tracks}, ip=actor.ip)
    return eid


def _save_prizes(conn: sqlite3.Connection, event_id: str, prizes: list[dict]) -> None:
    rows = conn.execute("SELECT id, name FROM tracks WHERE event_id = ?", (event_id,)).fetchall()
    track_ids = {r["id"] for r in rows}
    by_name = {r["name"].lower(): r["id"] for r in rows}
    existing = {r["name"].lower(): r["id"] for r in conn.execute(
        "SELECT id, name FROM prizes WHERE event_id = ?", (event_id,))}
    kept = set()
    for pos, prize in enumerate(prizes):
        name = clean_text(prize.get("name"), "prize name", max_len=120)
        if not name:
            continue
        track = _as_str(prize.get("track_id"), "prize track") or None
        if not track and prize.get("track_name"):
            track = by_name.get(str(prize["track_name"]).strip().lower())
            if track is None:
                raise Invalid(f"prize {name}: there is no track called {prize['track_name']}")
        if track and track not in track_ids:
            raise Invalid("prize track is not a track of this event")
        desc = clean_text(prize.get("description"), "prize description", max_len=500)
        pid = existing.get(name.lower())
        if pid:
            # Same name, same prize: keep its id so nothing that refers to it breaks.
            conn.execute("UPDATE prizes SET track_id = ?, description = ?, position = ? WHERE id = ?",
                         (track, desc, pos, pid))
        else:
            pid = new_id("prz")
            conn.execute(
                "INSERT INTO prizes(id, event_id, track_id, name, description, position) VALUES (?,?,?,?,?,?)",
                (pid, event_id, track, name, desc, pos),
            )
        kept.add(pid)
    for pid in set(existing.values()) - kept:
        conn.execute("DELETE FROM prizes WHERE id = ?", (pid,))


def update_event(conn: sqlite3.Connection, actor: Actor | None, event_id: str, form: dict,
                 new_tracks: list[str] | None = None, prizes: list[dict] | None = None) -> None:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        event = get_event(conn, event_id)
        values = _event_values(form, partial=dict(event))
        changed = {k: {"from": event[k], "to": v} for k, v in values.items() if event[k] != v}
        if event["results_published_at"]:
            current_prizes = [(p["name"], p["description"], p["track_id"]) for p in prizes_of(conn, event_id)]
            wanted = None if prizes is None else [
                (str(p.get("name") or "").strip(), str(p.get("description") or "").strip(), p.get("track_id"))
                for p in prizes if str(p.get("name") or "").strip()]
            if (set(changed) - {"tagline", "description"} or [t for t in (new_tracks or []) if t.strip()]
                    or (wanted is not None and [w[:2] for w in wanted] != [c[:2] for c in current_prizes])):
                raise Conflict("results are published; only the tagline and description can still change")
            prizes = None
        _check_schedule_change(conn, event, changed)
        if changed:
            conn.execute(
                f"UPDATE events SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                (*values.values(), event_id),
            )
        existing = {r["name"].lower() for r in conn.execute("SELECT name FROM tracks WHERE event_id = ?", (event_id,))}
        added = []
        for name in new_tracks or []:
            name = clean_text(name, "track", max_len=80)
            if name and name.lower() not in existing:
                conn.execute("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)", (new_id("trk"), event_id, name))
                existing.add(name.lower())
                added.append(name)
        if prizes is not None:
            _save_prizes(conn, event_id, prizes)
        if changed or added or prizes is not None:
            audit.append(conn, "event.updated", actor_id=actor.id, event_id=event_id, subject=event_id,
                         detail={"changed": changed, "tracks_added": added, "prizes_replaced": prizes is not None},
                         ip=actor.ip)


SCHEDULE_FIELDS = {"submissions_open", "submissions_close", "judging_close", "voting_mode", "voting_open",
                   "voting_close", "votes_per_voter", "pairwise"}


def _check_schedule_change(conn: sqlite3.Connection, event: sqlite3.Row, changed: dict) -> None:
    """Deadlines may move, but never in a way that reopens what was decided.

    * After publication the schedule is frozen: the signed record describes
      this event, and nothing may make the live event disagree with it.
    * Once any review exists, submissions cannot reopen (judges scored what
      they saw).
    * Once voting has closed, it cannot reopen (the counts have been visible).
    """
    if not set(changed) & SCHEDULE_FIELDS:
        return
    if event["results_published_at"]:
        raise Conflict("results are published; the schedule is frozen")
    now_ts = now()
    if "submissions_close" in changed and event["submissions_close"] <= now_ts < changed["submissions_close"]["to"]:
        if conn.execute("SELECT 1 FROM reviews WHERE event_id = ? UNION SELECT 1 FROM comparisons WHERE event_id = ?",
                        (event["id"], event["id"])).fetchone():
            raise Conflict("judges have already reviewed submissions; submissions cannot reopen")
    voting_open_now = (event["voting_mode"] != "off" and event["voting_open"] and event["voting_close"]
                       and event["voting_open"] <= now_ts < event["voting_close"])
    if voting_open_now:
        new_close = changed.get("voting_close", {}).get("to", event["voting_close"])
        new_mode = changed.get("voting_mode", {}).get("to", event["voting_mode"])
        if new_mode == "off" or new_close is None or new_close < event["voting_close"]:
            raise Conflict("voting is open; it can be extended but not shortened or switched off")
    ever_closed = (event["voting_close"] and event["voting_close"] <= now_ts
                   and conn.execute("SELECT 1 FROM votes WHERE event_id = ?", (event["id"],)).fetchone())
    if ever_closed and set(changed) & {"voting_mode", "voting_open", "voting_close", "votes_per_voter"}:
        raise Conflict("voting has closed and its counts are visible; its settings are frozen")


def tracks(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tracks WHERE event_id = ? ORDER BY name", (event_id,)).fetchall()


def prizes_of(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM prizes WHERE event_id = ? ORDER BY position", (event_id,)).fetchall()


def prizes(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, t.name AS track_name FROM prizes p LEFT JOIN tracks t ON t.id = p.track_id "
        "WHERE p.event_id = ? ORDER BY p.position", (event_id,)
    ).fetchall()


def criteria(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM criteria WHERE event_id = ? ORDER BY position, key", (event_id,)
    )]


def update_rubric(conn: sqlite3.Connection, actor: Actor | None, event_id: str, weights: dict[str, float],
                  new_criterion: dict | None = None) -> None:
    """Weights can change at any time before results are published (they are
    applied when scores are combined, so no stored review goes stale).
    Adding a criterion is only allowed before the first review, because
    earlier reviews would have no value for it."""
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        event = get_event(conn, event_id)
        if event["results_published_at"]:
            raise Conflict("results are published; the rubric is locked")
        if event["rubric_locked_at"]:
            raise Conflict(f"the rubric was locked at {event['rubric_locked_at']}")
        current = {c["id"]: c for c in criteria(conn, event_id)}
        changes = {}
        for cid, weight in weights.items():
            if cid not in current:
                raise Invalid("unknown criterion")
            try:
                w = float(weight)
            except (TypeError, ValueError) as exc:
                raise Invalid("weights must be numbers") from exc
            if not 0 <= w <= 100:
                raise Invalid("weights must be between 0 and 100")
            if abs(current[cid]["weight"] - w) > 1e-12:
                conn.execute("UPDATE criteria SET weight = ? WHERE id = ?", (w, cid))
                changes[current[cid]["key"]] = {"from": current[cid]["weight"], "to": w}
        remaining = {cid: float(weights.get(cid, c["weight"])) for cid, c in current.items()}
        if current and sum(remaining.values()) <= 0:
            raise Invalid("at least one criterion needs a weight above zero")
        added = None
        if new_criterion and (new_criterion.get("name") or "").strip():
            if conn.execute("SELECT 1 FROM reviews WHERE event_id = ?", (event_id,)).fetchone():
                raise Conflict("reviews already exist; criteria can be reweighted but not added")
            name = clean_text(new_criterion.get("name"), "criterion name", required=True, max_len=60)
            key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "criterion"
            if any(c["key"] == key for c in current.values()):
                raise Conflict("a criterion with that name exists")
            conn.execute(
                "INSERT INTO criteria(id, event_id, key, name, description, weight, position) VALUES (?,?,?,?,?,?,?)",
                (new_id("crt"), event_id, key, name,
                 clean_text(new_criterion.get("description"), "criterion description", max_len=300),
                 float(new_criterion.get("weight") or 1), len(current)),
            )
            added = key
        if changes or added:
            audit.append(conn, "rubric.updated", actor_id=actor.id, event_id=event_id, subject=event_id,
                         detail={"weights": changes, "added": added}, ip=actor.ip)


def lock_rubric(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> None:
    """Freeze the rubric for good, typically as judging opens, so nobody can
    steer the ranking by reweighting after seeing it. Irreversible."""
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        event = get_event(conn, event_id)
        if event["rubric_locked_at"]:
            raise Conflict("the rubric is already locked")
        conn.execute("UPDATE events SET rubric_locked_at = ? WHERE id = ?", (now(), event_id))
        audit.append(conn, "rubric.locked", actor_id=actor.id, event_id=event_id, subject=event_id,
                     detail={"weights": {c["key"]: c["weight"] for c in criteria(conn, event_id)}}, ip=actor.ip)


def rubric_history(conn: sqlite3.Connection, event_id: str) -> dict:
    """How the rubric was handled, for the signed record: was it locked, and
    how often was it reweighted after the first review existed."""
    event = get_event(conn, event_id)
    first = conn.execute("SELECT MIN(submitted_at) FROM reviews WHERE event_id = ?", (event_id,)).fetchone()[0]
    changes = 0
    if first:
        changes = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_id = ? AND action = 'rubric.updated' AND at >= ?",
            (event_id, first)).fetchone()[0]
    return {"locked_at": event["rubric_locked_at"], "changes_after_first_review": changes}
