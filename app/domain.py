"""Everything the portal can do, as plain functions over a connection.

The HTML routes and the JSON API both call these, so a rule enforced here is
enforced everywhere, whichever door a request came through. Each mutating
function runs in one transaction and writes its own audit entry.

Permission checks live here, not in templates. A function either returns
what the actor may see or raises Forbidden.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import assign, audit, normalize, pairwise
from .db import new_id, now, parse_ts, transaction
from .security import new_token, token_hash  # noqa: F401  (new_token also seals votes)


class DomainError(Exception):
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    status = 404


class Unauthorized(DomainError):
    status = 401


class Forbidden(DomainError):
    status = 403


class Conflict(DomainError):
    status = 409


class Invalid(DomainError):
    status = 422


@dataclass(frozen=True)
class Actor:
    id: str
    email: str
    name: str
    is_admin: bool
    ip: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- small validators --------------------------------------------------------

_URL = re.compile(r"^https?://[^\s<>\"]+$", re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Control characters, and the invisible bidi and zero-width marks that can
# make one team name look like another on a signed certificate.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _as_str(value, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    if _CONTROL.search(value):
        raise Invalid(f"{field} contains control characters")
    return value


def clean_text(value: str | None, field: str, *, required: bool = False, max_len: int = 200) -> str:
    value = _as_str(value, field).strip()
    if required and not value:
        raise Invalid(f"{field} is required")
    if len(value) > max_len:
        raise Invalid(f"{field} is longer than {max_len} characters")
    return value


def clean_url(value: str | None, field: str) -> str:
    value = _as_str(value, field).strip()
    if value and not _URL.match(value):
        raise Invalid(f"{field} must be an http or https URL")
    if len(value) > 500:
        raise Invalid(f"{field} is too long")
    return value


def clean_email(value: str | None) -> str:
    value = _as_str(value, "email").strip().lower()
    if not _EMAIL.match(value) or len(value) > 254:
        raise Invalid("that does not look like an email address")
    return value


def clean_ts(value: str | None, field: str, *, required: bool = False) -> str | None:
    """Accept ISO 8601 or an HTML datetime-local value (taken as UTC)."""
    value = _as_str(value, field).strip()
    if not value:
        if required:
            raise Invalid(f"{field} is required")
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid(f"{field} is not a date and time") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- users -------------------------------------------------------------------


def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def create_user(
    conn: sqlite3.Connection, email: str, name: str, password_hash: str | None, *,
    is_admin: bool = False, ip: str | None = None, user_id: str | None = None, actor_id: str | None = None,
    allow_claim: bool = False,
) -> str:
    """Create an account, or claim one an import created without a password.

    Claiming is only allowed through an invitation link (allow_claim=True).
    Plumb sends no mail, so it cannot prove someone owns an address; an
    organizer handing out the link is that proof. Without this rule anyone
    could sign up as an imported judge's email and inherit the judge role.
    """
    email = clean_email(email)
    name = clean_text(name, "name", required=True, max_len=100)
    with transaction(conn):
        existing = user_by_email(conn, email)
        if existing and (existing["password_hash"] or not allow_claim):
            if existing["password_hash"]:
                raise Conflict("an account with that email already exists; log in instead")
            raise Conflict("this email is already registered for an event; ask the organizer for your invitation link")
        if existing:
            # An account created by an import, claimed through an invitation.
            conn.execute(
                "UPDATE users SET name = ?, password_hash = ? WHERE id = ?", (name, password_hash, existing["id"])
            )
            audit.append(conn, "user.claimed", actor_id=existing["id"], subject=existing["id"], ip=ip)
            return existing["id"]
        uid = user_id or new_id("usr")
        conn.execute(
            "INSERT INTO users(id, email, name, password_hash, is_admin, created_at, created_ip) VALUES (?,?,?,?,?,?,?)",
            (uid, email, name, password_hash, int(is_admin), now(), ip),
        )
        audit.append(conn, "user.created", actor_id=actor_id or uid, subject=uid, detail={"admin": is_admin}, ip=ip)
        return uid


# --- events and roles -------------------------------------------------------


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


# --- teams -------------------------------------------------------------------


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


# --- projects ------------------------------------------------------------------


PROJECT_COLUMNS = (
    "p.*, t.name AS team_name, tr.name AS track_name, e.name AS event_name"
)
PROJECT_FROM = (
    "FROM projects p JOIN teams t ON t.id = p.team_id JOIN events e ON e.id = p.event_id "
    "LEFT JOIN tracks tr ON tr.id = p.track_id"
)


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
        state = phase(event)["submissions"]
        if state == "closed":
            raise Conflict(f"submissions for {event['name']} closed at {event['submissions_close']}")
        if state == "upcoming":
            raise Conflict(f"submissions for {event['name']} open at {event['submissions_open']}")
        team = team_for(conn, event_id, actor.id)
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
        if phase(event)["submissions"] != "open":
            raise Conflict("submissions are closed")
        if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (project["team_id"], actor.id)).fetchone():
            raise Forbidden("that is not your team's project")
        conn.execute("UPDATE projects SET status='draft', submitted_at=NULL, updated_at=? WHERE id=?", (now(), project_id))
        audit.append(conn, "project.unsubmitted", actor_id=actor.id, event_id=event["id"], subject=project_id, ip=actor.ip)


def gallery(conn: sqlite3.Connection, *, event_id: str | None = None, track_id: str | None = None,
            q: str | None = None, limit: int = 60, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    where = ["p.status = 'submitted'", "p.duplicate_of IS NULL"]
    args: list = []
    if event_id:
        where.append("p.event_id = ?")
        args.append(event_id)
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
        # Events in the order they were created (the demo's fixture event first),
        # then projects in id order: stable across pages and across restarts.
        f"SELECT {PROJECT_COLUMNS} {PROJECT_FROM} WHERE {clause} ORDER BY e.created_at, e.id, p.id LIMIT ? OFFSET ?",
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
                out.append({"first": a, "second": b, "reasons": reasons, "resolved": resolved})
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


# --- judges, invitations, assignment -----------------------------------------------


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


def may_claim_with(conn: sqlite3.Connection, inv: sqlite3.Row) -> bool:
    """Whether an invitation may set the password of the account it names.

    Holding the link proves the organizer vouches for the address, but only
    for their own event. An unclaimed account that already belongs to any
    other event (imported there as a judge or team member) can only be
    claimed through that event; otherwise an organizer anywhere could take
    over any imported judge by inviting their address.
    """
    user = user_by_email(conn, inv["email"])
    if user is None:
        return True
    if user["password_hash"]:
        return False
    elsewhere = conn.execute(
        "SELECT 1 FROM event_roles WHERE user_id = ? AND event_id <> ? "
        "UNION SELECT 1 FROM team_members WHERE user_id = ? AND event_id <> ?",
        (user["id"], inv["event_id"], user["id"], inv["event_id"]),
    ).fetchone()
    return elsewhere is None


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
            "SELECT id, track_id, team_id FROM projects WHERE event_id = ? AND status = 'submitted' "
            "AND duplicate_of IS NULL AND disqualified_reason IS NULL", (event_id,)
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


def judge_queue(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[sqlite3.Row]:
    actor = require_judge(conn, event_id, actor)
    return conn.execute(
        f"SELECT {PROJECT_COLUMNS}, v.id AS review_id, v.updated_at AS reviewed_at "
        f"{PROJECT_FROM} JOIN assignments a ON a.project_id = p.id "
        "LEFT JOIN reviews v ON v.project_id = p.id AND v.judge_id = a.judge_id "
        "WHERE a.event_id = ? AND a.judge_id = ? AND p.status = 'submitted' AND p.duplicate_of IS NULL "
        "AND p.disqualified_reason IS NULL ORDER BY (v.id IS NOT NULL), p.title",
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


def progress(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
    require_organizer(conn, event_id, actor)
    event = get_event(conn, event_id)
    target = event["reviews_per_project"]
    rows = conn.execute(
        "SELECT p.id, p.title, p.track_id, t.name AS team_name, "
        " (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) AS assigned, "
        " (SELECT COUNT(*) FROM reviews v WHERE v.project_id = p.id) AS reviewed "
        "FROM projects p JOIN teams t ON t.id = p.team_id WHERE p.event_id = ? AND p.status = 'submitted' "
        "AND p.duplicate_of IS NULL AND p.disqualified_reason IS NULL ORDER BY reviewed, p.id",
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
    }


# --- results -----------------------------------------------------------------------


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


# --- voting --------------------------------------------------------------------------


def ballot_order_key(voter_id: str, event_id: str, project_id: str) -> str:
    import hashlib
    return hashlib.sha256(f"{event_id}|{voter_id}|{project_id}".encode()).hexdigest()


def _voter_check(conn: sqlite3.Connection, event: sqlite3.Row, actor: Actor) -> None:
    mode = event["voting_mode"]
    if mode == "participants" and not team_for(conn, event["id"], actor.id):
        raise Forbidden("only participants of this event can vote")
    if "judge" in roles(conn, event["id"], actor):
        raise Forbidden("judges do not vote in the community ballot")


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
    rows = [r for r in rows if not r["disqualified_reason"]]
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
        if project["status"] != "submitted" or project["duplicate_of"] or project["disqualified_reason"]:
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
        # Sealed: the log proves a vote was cast and pins what it was, without
        # saying what it was until the nonces are revealed at close.
        audit.append(conn, "vote.cast", actor_id=actor.id, event_id=event["id"], subject="sealed",
                     detail={"seal": vote_seal(nonce, project_id)}, ip=actor.ip)


def vote_seal(nonce: str, project_id: str) -> str:
    import hashlib
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
        audit.append(conn, "vote.withdrawn", actor_id=actor.id, event_id=event["id"], subject="sealed",
                     detail={"seal": vote_seal(row["nonce"] or "", project_id)}, ip=actor.ip)


def vote_tallies(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[sqlite3.Row]:
    """Hidden from everyone, organizers included, until voting closes.
    Nobody can nudge a close race they can see."""
    event = get_event(conn, event_id)
    if phase(event)["voting"] != "closed":
        raise Forbidden("vote counts stay hidden until voting closes")
    return conn.execute(
        f"SELECT {PROJECT_COLUMNS}, COUNT(v.voter_id) AS votes {PROJECT_FROM} "
        "LEFT JOIN votes v ON v.project_id = p.id "
        "WHERE p.event_id = ? AND p.status = 'submitted' AND p.duplicate_of IS NULL AND p.disqualified_reason IS NULL "
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
    actor = require_organizer(conn, event_id, actor)
    reason = clean_text(reason, "reason", required=True, max_len=300)
    with transaction(conn):
        cur = conn.execute("DELETE FROM votes WHERE event_id = ? AND voter_id = ?", (event_id, voter_id))
        audit.append(conn, "votes.voided", actor_id=actor.id, event_id=event_id, subject=voter_id,
                     detail={"removed": cur.rowcount, "reason": reason}, ip=actor.ip)
    return cur.rowcount


# --- comments -------------------------------------------------------------------------


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


# --- pairwise judging -----------------------------------------------------------------


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


def pairwise_next(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> dict:
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
    pair = pairwise.next_pair(assigned, done, current)
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


# --- webhooks --------------------------------------------------------------------------


def webhooks_for(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[dict]:
    require_organizer(conn, event_id, actor)
    out = []
    for h in conn.execute("SELECT * FROM webhooks WHERE event_id = ? ORDER BY created_at", (event_id,)):
        last = conn.execute("SELECT * FROM webhook_deliveries WHERE webhook_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                            (h["id"],)).fetchone()
        out.append({**{k: h[k] for k in ("id", "url", "cursor", "created_at")}, "last": dict(last) if last else None})
    return out


def create_webhook(conn: sqlite3.Connection, actor: Actor | None, event_id: str, url: str, check) -> dict:
    """check(url) raises ValueError/OSError for a destination that is not allowed."""
    actor = require_organizer(conn, event_id, actor)
    url = clean_url(url, "url")
    if not url:
        raise Invalid("url is required")
    try:
        check(url)
    except (ValueError, OSError) as exc:
        raise Invalid(f"webhook url refused: {exc}") from exc
    import secrets as _secrets
    hid, secret = new_id("whk"), _secrets.token_hex(24)
    with transaction(conn):
        conn.execute(
            "INSERT INTO webhooks(id, event_id, url, secret, cursor, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (hid, event_id, url, secret, audit.head(conn)["seq"], actor.id, now()),
        )
        audit.append(conn, "webhook.created", actor_id=actor.id, event_id=event_id, subject=hid, detail={"url": url}, ip=actor.ip)
    return {"id": hid, "url": url, "secret": secret}


def delete_webhook(conn: sqlite3.Connection, actor: Actor | None, event_id: str, hook_id: str) -> None:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        cur = conn.execute("DELETE FROM webhooks WHERE id = ? AND event_id = ?", (hook_id, event_id))
        if cur.rowcount == 0:
            raise NotFound("no such webhook")
        audit.append(conn, "webhook.deleted", actor_id=actor.id, event_id=event_id, subject=hook_id, ip=actor.ip)


# --- exports ---------------------------------------------------------------------------


def _cell(value):
    """Neutralise spreadsheet formulas: a cell a participant controls must
    never execute when an organizer opens the export."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class _SafeWriter:
    def __init__(self, buf):
        self._w = csv.writer(buf)

    def writerow(self, row):
        self._w.writerow([_cell(v) for v in row])


def scores_csv(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> str:
    require_organizer(conn, event_id, actor)
    crit = criteria(conn, event_id)
    buf = io.StringIO()
    w = _SafeWriter(buf)
    w.writerow(["event_id", "project_id", "project_title", "team_id", "track_id", "judge_id", "judge_name",
                *[c["key"] for c in crit], "weighted_score_0_100", "eligible", "comment", "submitted_at", "updated_at"])
    for r in reviews_for(conn, event_id=event_id):
        score = normalize.combine(r["values"], crit)
        eligible = not (r["duplicate_of"] or r["disqualified_reason"] or r["project_status"] != "submitted")
        w.writerow([event_id, r["project_id"], r["project_title"], r["team_id"], r["track_id"] or "", r["judge_id"],
                    r["judge_name"], *[r["values"].get(c["key"], "") for c in crit],
                    "" if score is None else f"{score:.2f}", "yes" if eligible else "no",
                    r["comment"], r["submitted_at"], r["updated_at"]])
    return buf.getvalue()


def results_csv(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> str:
    res = results_for_viewer(conn, actor, event_id)
    buf = io.StringIO()
    w = _SafeWriter(buf)
    w.writerow(["rank", "project_id", "project_title", "team", "track", "reviews", "raw_mean_0_100", "raw_rank",
                "adjusted_0_100", "interval90_low", "interval90_high", "p_beats_next"])
    for p in res["fit"].projects:
        proj = res["projects"][p.project]
        w.writerow([p.rank, p.project, proj["title"], proj["team_name"], proj["track_name"] or "", p.n_reviews,
                    f"{p.raw_mean:.2f}", p.raw_rank, f"{p.adjusted:.2f}", f"{p.low:.2f}", f"{p.high:.2f}",
                    "" if p.p_above_next is None else f"{p.p_above_next:.3f}"])
    return buf.getvalue()


def audit_entries(conn: sqlite3.Connection, actor: Actor | None, event_id: str, *, limit: int = 200,
                  before: int | None = None, action: str | None = None) -> list[sqlite3.Row]:
    require_organizer(conn, event_id, actor)
    where, args = ["a.event_id = ?"], [event_id]
    if before:
        where.append("a.seq < ?")
        args.append(before)
    if action:
        where.append("a.action LIKE ?")
        args.append(action.replace("%", "") + "%")
    return conn.execute(
        "SELECT a.*, u.name AS actor_name, u.email AS actor_email FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.seq DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
