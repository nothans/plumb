"""Signed records: what the portal attests to, in a form anyone can check.

Publishing results issues three kinds of record, each a canonical JSON
payload signed with the portal's Ed25519 key:

* results   - the ranking, the method and its fitted parameters, the rubric
              weights, and the audit-log head at the moment of publication.
* judge     - that a judge took part, how many reviews they filed, and a
              SHA-256 digest of those reviews. The judge can recompute the
              digest from their own score export, so the record commits the
              portal to exactly what the judge submitted, without revealing
              the scores to anyone else.
* team      - that a team took part, with its project, rank and interval;
              the certificate page renders it.

A record never changes after it is issued. The audit-log head inside it
pins the whole history up to that moment: rewrite any earlier audit row and
the chain no longer reaches the hash the record carries.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

from . import audit, canonical, domain
from .db import new_id, now, transaction
from .signing import Signer, verify


def _review_digest(reviews: list[dict]) -> str:
    items = sorted(
        ({"project": r["project_id"], "values": r["values"], "comment": r["comment"], "updated_at": r["updated_at"]}
         for r in reviews),
        key=lambda x: x["project"],
    )
    return hashlib.sha256(canonical.dumps(items).encode()).hexdigest()


def review_digest_for(conn: sqlite3.Connection, event_id: str, judge_id: str) -> str:
    return _review_digest(domain.reviews_for(conn, event_id=event_id, judge_id=judge_id))


def _issue(conn: sqlite3.Connection, signer: Signer, event_id: str, kind: str, subject: str, body: dict,
           base_url: str) -> str:
    rid = new_id("rec")
    payload = canonical.dumps({
        "id": rid,
        "type": f"plumb.{kind}/v1",
        "issuer": base_url,
        "key_id": signer.key_id,
        "issued_at": now(),
        **body,
    })
    conn.execute(
        "INSERT INTO records(id, event_id, kind, subject, payload, signature, key_id, issued_at) VALUES (?,?,?,?,?,?,?,?)",
        (rid, event_id, kind, subject, payload, signer.sign(payload), signer.key_id, now()),
    )
    return rid


def publish_results(conn: sqlite3.Connection, actor: domain.Actor | None, event_id: str, signer: Signer,
                    base_url: str, awards: dict[str, str] | None = None) -> dict:
    """Sign and freeze the results. awards maps prize id to project id; a
    prize left out gets the suggested default, and an empty string means
    "not awarded"."""
    actor = domain.require_organizer(conn, event_id, actor)
    with transaction(conn):
        event = domain.get_event(conn, event_id)
        if event["results_published_at"]:
            raise domain.Conflict("results are already published")
        ph = domain.phase(event)
        if ph["submissions"] != "closed":
            raise domain.Conflict("submissions are still open")
        if ph["voting"] in ("open", "upcoming"):
            raise domain.Conflict(f"the community vote runs until {event['voting_close']}; publish after it closes, "
                                  "so judged results cannot sway it")
        if not base_url:
            raise domain.Conflict("set PLUMB_BASE_URL to the portal's public address first: it is signed into every record")
        res = domain.compute_results(conn, event_id)
        fit = res["fit"]
        if not fit.projects:
            raise domain.Conflict("there are no reviews to publish")
        if not fit.identifiable:
            raise domain.Conflict("there are not enough reviews to estimate how uncertain the ranking is "
                                  "(the event needs more reviews than projects); assign more judges first")
        ranked = {p.project for p in fit.projects}
        chosen = []
        overall_taken: set[str] = set()
        for suggestion in domain.default_awards(conn, event_id, fit, res["projects"]):
            prize = suggestion["prize"]
            if prize["track_id"]:
                top = suggestion["project"]
            else:
                top = next((p.project for p in fit.projects if p.project not in overall_taken), None)
            pick = (awards or {}).get(prize["id"], top)
            if pick and not prize["track_id"] and pick in overall_taken:
                raise domain.Invalid(f"{res['projects'][pick]['title']} already won an overall prize")
            if not pick:
                continue
            if pick not in ranked:
                raise domain.Invalid(f"{prize['name']}: that project is not in the ranking")
            if prize["track_id"] and res["projects"][pick]["track_id"] != prize["track_id"]:
                raise domain.Invalid(f"{prize['name']} is for the {prize['track_name']} track")
            # The closest rival is the best other project this prize could
            # have gone to: for a track prize, anyone in the track; for an
            # overall prize, anyone who has not already won an overall prize.
            if prize["track_id"]:
                group = [p.project for p in fit.projects if res["projects"][p.project]["track_id"] == prize["track_id"]]
            else:
                group = [p.project for p in fit.projects if p.project not in overall_taken]
                overall_taken.add(pick)
            rival = next((p for p in group if p != pick), None)
            chosen.append({"prize": prize["id"], "name": prize["name"], "track": prize["track_id"], "project": pick,
                           "title": res["projects"][pick]["title"], "team": res["projects"][pick]["team_id"],
                           "rank": fit.project(pick).rank, "rival": rival,
                           "p_beats_rival": None if rival is None else round(fit.p_better(pick, rival), 4),
                           "suggested": top, "overrode_suggestion": pick != top})
        head = audit.head(conn)
        ev = {"id": event["id"], "name": event["name"]}
        ranking = []
        for p in fit.projects:
            proj = res["projects"][p.project]
            ranking.append({
                "rank": p.rank, "project": p.project, "title": proj["title"], "team": proj["team_id"],
                "track": proj["track_id"], "reviews": p.n_reviews, "adjusted": round(p.adjusted, 2),
                "interval90": [round(p.low, 2), round(p.high, 2)], "raw_mean": round(p.raw_mean, 2),
                "raw_rank": p.raw_rank,
                "p_beats_next": None if p.p_above_next is None else round(p.p_above_next, 4),
            })
        results_id = _issue(conn, signer, event_id, "results", event_id, {
            "event": ev,
            "method": {
                "model": "score = quality[project] + leniency[judge] + noise; projects fixed, judges random; REML",
                "judge_sd": round(fit.judge_sd, 4), "noise_sd": round(fit.noise_sd, 4),
                "spread_sd": round(fit.spread_sd, 4),
                "observations": fit.n_observations, "graph_components": fit.components,
            },
            "rubric": [{"key": c["key"], "weight": c["weight"], "min": c["min_value"], "max": c["max_value"]}
                       for c in res["criteria"]],
            "rubric_history": domain.rubric_history(conn, event_id),
            "awards": chosen,
            "ranking": ranking,
            "excluded_reviews": len(res["excluded"]),
            "audit_head": head,
        }, base_url)

        judge_records = 0
        for j in domain.judges(conn, event_id):
            reviews = domain.reviews_for(conn, event_id=event_id, judge_id=j["id"])
            if not reviews:
                continue
            _issue(conn, signer, event_id, "judge", j["id"], {
                "event": ev,
                "judge": {"id": j["id"], "name": j["name"]},
                "reviews": len(reviews),
                "review_digest": _review_digest(reviews),
                "results_record": results_id,
                "audit_head": head,
            }, base_url)
            judge_records += 1

        by_project = {p.project: p for p in fit.projects}
        team_records = 0
        for t in conn.execute("SELECT * FROM teams WHERE event_id = ? ORDER BY id", (event_id,)).fetchall():
            proj = conn.execute(
                "SELECT * FROM projects WHERE team_id = ? AND duplicate_of IS NULL AND status = 'submitted'", (t["id"],)
            ).fetchone()
            if proj is None:
                continue
            members = [m["name"] for m in domain.team_members(conn, t["id"])]
            placed = by_project.get(proj["id"])
            won = [a["name"] for a in chosen if a["project"] == proj["id"]]
            _issue(conn, signer, event_id, "team", t["id"], {
                "event": ev,
                "team": {"id": t["id"], "name": t["name"], "members": members},
                "project": {"id": proj["id"], "title": proj["title"]},
                "placement": None if placed is None else {
                    "rank": placed.rank, "of": len(fit.projects), "adjusted": round(placed.adjusted, 2),
                    "interval90": [round(placed.low, 2), round(placed.high, 2)],
                },
                "awards": won,
                "results_record": results_id,
            }, base_url)
            team_records += 1

        for a in chosen:
            conn.execute("INSERT INTO awards(prize_id, event_id, project_id, awarded_at) VALUES (?,?,?,?)",
                         (a["prize"], event_id, a["project"], now()))
        conn.execute("UPDATE events SET results_published_at = ? WHERE id = ?", (now(), event_id))
        audit.append(conn, "results.published", actor_id=actor.id, event_id=event_id, subject=results_id,
                     detail={"judge_records": judge_records, "team_records": team_records, "audit_head": head,
                             "awards": {a["name"]: a["project"] for a in chosen}},
                     ip=actor.ip)
    return {"results_record": results_id, "judge_records": judge_records, "team_records": team_records}


def published_results(conn: sqlite3.Connection, event_id: str) -> dict | None:
    """The public results, read back from the signed record rather than
    recomputed, so the page shows exactly what was signed. Shaped like
    domain.compute_results so the same templates render it."""
    row = conn.execute(
        "SELECT * FROM records WHERE event_id = ? AND kind = 'results' ORDER BY issued_at DESC, id DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    body = json.loads(row["payload"])
    m = body["method"]
    projects = []
    for r in body["ranking"]:
        low, high = r["interval90"]
        projects.append(SimpleNamespace(
            project=r["project"], rank=r["rank"], raw_rank=r.get("raw_rank", r["rank"]), n_reviews=r["reviews"],
            raw_mean=r["raw_mean"], adjusted=r["adjusted"], low=low, high=high, p_above_next=r.get("p_beats_next"),
        ))
    fit = SimpleNamespace(
        projects=projects, n_observations=m["observations"], judge_sd=m["judge_sd"], noise_sd=m["noise_sd"],
        spread_sd=m.get("spread_sd", 0.0), components=m["graph_components"], identifiable=True,
    )
    live = {r["id"]: r for r in conn.execute(
        f"SELECT {domain.PROJECT_COLUMNS} {domain.PROJECT_FROM} WHERE p.event_id = ?", (event_id,)
    )}
    # Titles and teams come from the record; the live row only adds names for display.
    names = {r["project"]: {"title": r["title"], "team_name": live[r["project"]]["team_name"] if r["project"] in live else r["team"],
                            "track_id": r["track"]} for r in body["ranking"]}
    return {"fit": fit, "projects": names, "record": row, "body": body}


def get_record(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise domain.NotFound("no such record")
    return row


def records_for(conn: sqlite3.Connection, event_id: str, kind: str | None = None) -> list[sqlite3.Row]:
    if kind:
        return conn.execute("SELECT * FROM records WHERE event_id = ? AND kind = ? ORDER BY issued_at, id",
                            (event_id, kind)).fetchall()
    return conn.execute("SELECT * FROM records WHERE event_id = ? ORDER BY issued_at, id", (event_id,)).fetchall()


def envelope(row: sqlite3.Row, signer: Signer) -> dict:
    """The portable form: everything needed to verify, in one object."""
    return {"payload": row["payload"], "signature": row["signature"], "key_id": row["key_id"],
            "public_key": signer.public_key_b64 if row["key_id"] == signer.key_id else None}


def check_envelope(env: dict, signer: Signer) -> dict:
    """Verify a pasted envelope against this portal's key.

    Uses this portal's published key, never a key inside the envelope: a
    forger can always sign with a key of their own.
    """
    payload, signature = env.get("payload"), env.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str):
        return {"ok": False, "reason": "the envelope needs a payload string and a signature"}
    if env.get("key_id") and env["key_id"] != signer.key_id:
        return {"ok": False, "reason": f"signed by key {env['key_id']}, but this portal's key is {signer.key_id}"}
    if not verify(payload, signature, signer.public_key_b64):
        return {"ok": False, "reason": "the signature does not match the payload; it was altered or not issued here"}
    import json
    try:
        body = json.loads(payload)
    except ValueError:
        return {"ok": False, "reason": "the payload is not JSON"}
    return {"ok": True, "reason": "signature valid", "body": body}
