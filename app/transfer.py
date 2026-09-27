"""Bulk import and export in the DOGFOOD fixture shape.

The format is the one fixtures.json uses (event, tracks, judges, teams,
projects, scores), so any event exported from Plumb can be loaded into
another Plumb, and any tool that reads the fixture shape can read Plumb's
exports. Plumb-specific data travels in an optional "plumb" block that other
readers can ignore.

The demo seed is not special code: it is this importer, fed fixtures.json.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from datetime import timedelta

from . import audit, domain
from .db import new_id, now, parse_ts, to_ts, transaction
from .security import new_token


def _user_id_for(email: str) -> str:
    return "usr_" + hashlib.sha256(email.lower().encode()).hexdigest()[:12]


def _ensure_user(conn: sqlite3.Connection, email: str, name: str, preferred_id: str | None = None) -> str:
    email = email.strip().lower()
    row = domain.user_by_email(conn, email)
    if row:
        return row["id"]
    uid = preferred_id or _user_id_for(email)
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        uid = _user_id_for(email)
    conn.execute(
        "INSERT INTO users(id, email, name, password_hash, is_admin, created_at) VALUES (?,?,?,NULL,0,?)",
        (uid, email, name, now()),
    )
    return uid


def _need(obj: dict, key: str, where: str):
    if key not in obj or obj[key] in (None, ""):
        raise domain.Invalid(f"{where} is missing '{key}'")
    return obj[key]


def import_event(conn: sqlite3.Connection, data: dict, *, actor_id: str | None, organizer_ids: list[str] = (),
                 event_id: str | None = None) -> dict:
    """Load a fixture-shaped document as a new event. All or nothing."""
    if not isinstance(data, dict):
        raise domain.Invalid("expected a JSON object")
    ev = data.get("event") or {}
    eid = event_id or _need(ev, "id", "event")
    name = _need(ev, "name", "event")
    close = domain.clean_ts(_need(ev, "submissions_close", "event"), "submissions_close", required=True)
    plumb = data.get("plumb") or {}
    pev = plumb.get("event") or {}
    for p in data.get("projects", []):
        if not isinstance(p, dict) or not p.get("id"):
            raise domain.Invalid("every project needs an id")
        domain.clean_ts(p.get("submitted_at"), f"project {p['id']} submitted_at")
    submitted = [domain.clean_ts(p.get("submitted_at"), "submitted_at") for p in data.get("projects", [])
                 if p.get("submitted_at")]
    # The fixture shape has no opening time. Use the one Plumb exported, or
    # else thirty days before the close (or before the first submission).
    default_open = min([parse_ts(close) - timedelta(days=30)] + [parse_ts(s) - timedelta(hours=1) for s in submitted])
    opens = domain.clean_ts(pev.get("submissions_open") or to_ts(default_open), "submissions_open", required=True)

    problems: list[str] = []
    track_ids = {t["id"] for t in data.get("tracks", []) if t.get("id")}
    judge_ids = {j["id"] for j in data.get("judges", []) if j.get("id")}
    team_ids = {t["id"] for t in data.get("teams", []) if t.get("id")}
    project_ids = {p["id"] for p in data.get("projects", []) if p.get("id")}
    for j in data.get("judges", []):
        problems += [f"judge {j.get('id')} lists unknown track {t}" for t in j.get("tracks", []) if t not in track_ids]
    for p in data.get("projects", []):
        if p.get("team") not in team_ids:
            problems.append(f"project {p.get('id')} belongs to unknown team {p.get('team')}")
        if p.get("track") and p["track"] not in track_ids:
            problems.append(f"project {p.get('id')} is in unknown track {p.get('track')}")
    seen_pairs = set()
    for i, s in enumerate(data.get("scores", [])):
        if s.get("judge") not in judge_ids:
            problems.append(f"score {i} is by unknown judge {s.get('judge')}")
        if s.get("project") not in project_ids:
            problems.append(f"score {i} is for unknown project {s.get('project')}")
        pair = (s.get("judge"), s.get("project"))
        if pair in seen_pairs:
            problems.append(f"score {i} repeats judge {pair[0]} on project {pair[1]}")
        seen_pairs.add(pair)
        if not isinstance(s.get("criteria"), dict) or not s["criteria"]:
            problems.append(f"score {i} has no criteria")
    if problems:
        raise domain.Invalid("the file has problems: " + "; ".join(problems[:10]) + ("; ..." if len(problems) > 10 else ""))

    counts = defaultdict(int)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM events WHERE id = ?", (eid,)).fetchone():
            raise domain.Conflict(f"an event with id {eid} already exists")
        values = domain._event_values({
            "name": name,
            "tagline": pev.get("tagline", ""),
            "description": pev.get("description", ""),
            "submissions_open": opens,
            "submissions_close": close,
            "judging_close": pev.get("judging_close"),
            "voting_mode": pev.get("voting_mode", "off"),
            "voting_open": pev.get("voting_open"),
            "voting_close": pev.get("voting_close"),
            "votes_per_voter": pev.get("votes_per_voter", 3),
            "reviews_per_project": pev.get("reviews_per_project", 3),
            "pairwise": pev.get("pairwise", 0),
            "max_team_size": max([4] + [len(t.get("members", [])) for t in data.get("teams", [])]),
        })
        conn.execute(
            f"INSERT INTO events(id, {', '.join(values)}, created_at) VALUES (?, {', '.join('?' * len(values))}, ?)",
            (eid, *values.values(), now()),
        )
        # Ids from the file are kept when they are free. One that is taken
        # (importing a copy of an event, or two files that both say trk_01)
        # gets a fresh id, and every reference inside the file follows it.
        def fresh(table: str, prefix: str, ids) -> dict[str, str]:
            out = {}
            for old in ids:
                taken = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (old,)).fetchone()
                out[old] = new_id(prefix) if taken else old
            return out

        trk = fresh("tracks", "trk", track_ids)
        tm = fresh("teams", "tm", team_ids)
        prj = fresh("projects", "prj", project_ids)
        renamed = sum(1 for m in (trk, tm, prj) for k, v in m.items() if k != v)

        for t in data.get("tracks", []):
            conn.execute("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)", (trk[t["id"]], eid, t["name"]))
            counts["tracks"] += 1
        for pos, prize in enumerate(plumb.get("prizes", [])):
            conn.execute(
                "INSERT INTO prizes(id, event_id, track_id, name, description, position) VALUES (?,?,?,?,?,?)",
                (new_id("prz"), eid, trk.get(prize.get("track")) if prize.get("track") else None, prize["name"], prize.get("description", ""), pos),
            )

        # Criteria: Plumb's own definitions if present, else every key the
        # scores use, weight 1, on a 1-5 scale widened to fit the data.
        crit_defs = plumb.get("criteria")
        if not crit_defs:
            keys: list[str] = []
            lo, hi = 1, 5
            for s in data.get("scores", []):
                for k, v in s["criteria"].items():
                    if k not in keys:
                        keys.append(k)
                    lo, hi = min(lo, int(v)), max(hi, int(v))
            crit_defs = [{"key": k, "name": k.replace("_", " ").capitalize(), "weight": 1, "min": lo, "max": hi} for k in keys]
        crit_ids = {}
        for pos, c in enumerate(crit_defs):
            cid = new_id("crt")
            crit_ids[c["key"]] = cid
            conn.execute(
                "INSERT INTO criteria(id, event_id, key, name, description, weight, min_value, max_value, position) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, eid, c["key"], c.get("name", c["key"]), c.get("description", ""), float(c.get("weight", 1)),
                 int(c.get("min", 1)), int(c.get("max", 5)), pos),
            )

        judge_user = {}
        for j in data.get("judges", []):
            uid = _ensure_user(conn, j["email"], j.get("name") or j["email"], preferred_id=j["id"])
            judge_user[j["id"]] = uid
            conn.execute("INSERT OR IGNORE INTO event_roles(event_id, user_id, role) VALUES (?,?, 'judge')", (eid, uid))
            for t in j.get("tracks", []):
                conn.execute("INSERT INTO judge_tracks(event_id, user_id, track_id) VALUES (?,?,?)", (eid, uid, trk[t]))
            counts["judges"] += 1

        names = defaultdict(list)
        for t in data.get("teams", []):
            names[t["name"].strip().lower()].append(t["id"])
        shared_names = [ids for ids in names.values() if len(ids) > 1]
        for t in data.get("teams", []):
            conn.execute(
                "INSERT INTO teams(id, event_id, name, invite_code, created_at) VALUES (?,?,?,?,?)",
                (tm[t["id"]], eid, t["name"], new_token(12), now()),
            )
            for email in t.get("members", []):
                uid = _ensure_user(conn, email, email.split("@")[0])
                if uid in judge_user.values():
                    raise domain.Invalid(f"{email} is both a judge and on team {t['id']}")
                conn.execute(
                    "INSERT INTO team_members(team_id, event_id, user_id, joined_at) VALUES (?,?,?,?)",
                    (tm[t["id"]], eid, uid, now()),
                )
            counts["teams"] += 1

        # Which entry of a team is live: a Plumb export says so explicitly
        # (the organizer may have swapped it); otherwise the earliest
        # submission is the entry and later ones by the same team are
        # recorded as its duplicates for an organizer to review.
        explicit = {d["project"]: d["duplicate_of"] for d in plumb.get("duplicates", [])}
        ordered = sorted(data.get("projects", []), key=lambda p: (p.get("submitted_at") or "9999", p["id"]))
        dup_of_old: dict[str, str | None] = {}
        first_by_team: dict[str, str] = {}
        for p in ordered:
            if p["id"] in explicit:
                dup_of_old[p["id"]] = explicit[p["id"]]
            elif explicit:
                dup_of_old[p["id"]] = None
            else:
                dup_of_old[p["id"]] = first_by_team.get(p["team"])
                first_by_team.setdefault(p["team"], p["id"])
        duplicates = []
        # Live entries first, so a duplicate's target always exists when it is inserted.
        for p in sorted(ordered, key=lambda p: dup_of_old[p["id"]] is not None):
            pid = prj[p["id"]]
            dup_of = prj.get(dup_of_old[p["id"]]) if dup_of_old[p["id"]] else None
            submitted_at = domain.clean_ts(p.get("submitted_at"), "submitted_at")
            status = p.get("status") or ("submitted" if submitted_at else "draft")
            if status not in ("draft", "submitted") or (status == "submitted" and not submitted_at):
                raise domain.Invalid(f"project {p['id']} has an invalid status or no submitted_at")
            conn.execute(
                "INSERT INTO projects(id, event_id, team_id, track_id, title, summary, description, repo_url, demo_url, "
                "status, duplicate_of, disqualified_reason, submitted_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, eid, tm[p["team"]], trk.get(p.get("track")) if p.get("track") else None, p["title"],
                 p.get("summary", ""), p.get("description", ""), p.get("repo_url", ""), p.get("demo_url", ""),
                 status, dup_of, p.get("disqualified_reason"), submitted_at,
                 p.get("created_at") or submitted_at or now(), p.get("updated_at") or submitted_at or now()),
            )
            if dup_of:
                duplicates.append({"project": pid, "duplicate_of": dup_of})
            counts["projects"] += 1

        ts = now()
        for s in data.get("scores", []):
            uid = judge_user[s["judge"]]
            conn.execute(
                "INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) VALUES (?,?,?, 'import', ?)",
                (eid, uid, prj[s["project"]], ts),
            )
            rid = new_id("rev")
            conn.execute(
                "INSERT INTO reviews(id, event_id, judge_id, project_id, comment, submitted_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (rid, eid, uid, prj[s["project"]], s.get("comment") or "", s.get("submitted_at") or ts, s.get("updated_at") or ts),
            )
            for key, value in s["criteria"].items():
                if key not in crit_ids:
                    raise domain.Invalid(f"score uses criterion '{key}' that the file does not define")
                conn.execute("INSERT INTO review_scores(review_id, criterion_id, value) VALUES (?,?,?)",
                             (rid, crit_ids[key], int(value)))
            counts["scores"] += 1

        for a in plumb.get("assignments", []):
            conn.execute(
                "INSERT OR IGNORE INTO assignments(event_id, judge_id, project_id, source, created_at) VALUES (?,?,?,?,?)",
                (eid, judge_user.get(a["judge"], a["judge"]), prj.get(a["project"], a["project"]), a.get("source", "import"), ts),
            )

        for c in plumb.get("comparisons", []):
            a, b = sorted((prj.get(c["a"], c["a"]), prj.get(c["b"], c["b"])))
            conn.execute(
                "INSERT INTO comparisons(event_id, judge_id, project_a, project_b, winner, created_at) VALUES (?,?,?,?,?,?)",
                (eid, judge_user.get(c["judge"], c["judge"]), a, b, prj.get(c["winner"], c["winner"]), c.get("at") or ts),
            )
            counts["comparisons"] += 1

        for uid in organizer_ids:
            conn.execute("INSERT OR IGNORE INTO event_roles(event_id, user_id, role) VALUES (?,?, 'organizer')", (eid, uid))
        audit.append(conn, "event.imported", actor_id=actor_id, event_id=eid, subject=eid,
                     detail={**counts, "duplicates": duplicates, "teams_sharing_a_name": shared_names,
                             "ids_renamed": renamed})
    return {"event_id": eid, **counts, "duplicates": duplicates, "teams_sharing_a_name": shared_names,
            "ids_renamed": renamed}


def export_event(conn: sqlite3.Connection, actor: domain.Actor | None, event_id: str) -> dict:
    domain.require_organizer(conn, event_id, actor)
    event = domain.get_event(conn, event_id)
    judges = conn.execute(
        "SELECT u.id, u.name, u.email FROM event_roles r JOIN users u ON u.id = r.user_id "
        "WHERE r.event_id = ? AND r.role = 'judge' ORDER BY u.id", (event_id,)
    ).fetchall()
    jt = defaultdict(list)
    for r in conn.execute("SELECT user_id, track_id FROM judge_tracks WHERE event_id = ? ORDER BY track_id", (event_id,)):
        jt[r["user_id"]].append(r["track_id"])
    members = defaultdict(list)
    for r in conn.execute(
        "SELECT m.team_id, u.email FROM team_members m JOIN users u ON u.id = m.user_id WHERE m.event_id = ? ORDER BY m.joined_at",
        (event_id,),
    ):
        members[r["team_id"]].append(r["email"])
    crit = domain.criteria(conn, event_id)
    return {
        "event": {"id": event["id"], "name": event["name"], "submissions_close": event["submissions_close"]},
        "tracks": [{"id": t["id"], "name": t["name"]} for t in domain.tracks(conn, event_id)],
        "judges": [{"id": j["id"], "name": j["name"], "email": j["email"], "tracks": jt[j["id"]]} for j in judges],
        "teams": [{"id": t["id"], "name": t["name"], "members": members[t["id"]]}
                  for t in conn.execute("SELECT * FROM teams WHERE event_id = ? ORDER BY id", (event_id,))],
        # Every project, drafts included, with every field: a reviewed entry
        # that went back to draft must still round-trip with its reviews.
        "projects": [
            {"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"], "summary": p["summary"],
             "description": p["description"], "repo_url": p["repo_url"], "demo_url": p["demo_url"],
             "status": p["status"], "disqualified_reason": p["disqualified_reason"],
             "submitted_at": p["submitted_at"], "created_at": p["created_at"], "updated_at": p["updated_at"]}
            for p in conn.execute("SELECT * FROM projects WHERE event_id = ? ORDER BY id", (event_id,))
        ],
        "scores": [
            {"judge": r["judge_id"], "project": r["project_id"], "criteria": r["values"], "comment": r["comment"],
             "submitted_at": r["submitted_at"], "updated_at": r["updated_at"]}
            for r in domain.reviews_for(conn, event_id=event_id)
        ],
        "plumb": {
            "format": "plumb.event/v1",
            "exported_at": now(),
            "event": {k: event[k] for k in (
                "tagline", "description", "submissions_open", "judging_close", "voting_mode", "voting_open",
                "voting_close", "votes_per_voter", "reviews_per_project", "max_team_size", "pairwise")},
            "criteria": [{"key": c["key"], "name": c["name"], "description": c["description"], "weight": c["weight"],
                          "min": c["min_value"], "max": c["max_value"]} for c in crit],
            "prizes": [{"name": p["name"], "description": p["description"], "track": p["track_id"]}
                       for p in domain.prizes(conn, event_id)],
            "assignments": [{"judge": a["judge_id"], "project": a["project_id"], "source": a["source"]}
                            for a in conn.execute("SELECT * FROM assignments WHERE event_id = ? ORDER BY judge_id, project_id", (event_id,))],
            "duplicates": [{"project": p["id"], "duplicate_of": p["duplicate_of"]}
                           for p in conn.execute("SELECT id, duplicate_of FROM projects WHERE event_id = ? AND duplicate_of IS NOT NULL", (event_id,))],
            "comparisons": [{"judge": c["judge_id"], "a": c["project_a"], "b": c["project_b"], "winner": c["winner"],
                             "at": c["created_at"]}
                            for c in conn.execute("SELECT * FROM comparisons WHERE event_id = ? ORDER BY created_at", (event_id,))],
            "audit_head": audit.head(conn),
        },
    }
