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
import secrets
import sqlite3
from types import SimpleNamespace

from . import audit, canonical, domain, method, normalize
from .db import new_id, now, transaction
from .signing import Signer, verify


def _review_digest(reviews: list[dict], salt: str | None = None) -> str:
    """sha256 over the judge's reviews in canonical JSON, prefixed with
    "<salt>:" when the record is salted (every record since v1.1)."""
    items = sorted(
        ({"project": r["project_id"], "values": r["values"], "comment": r["comment"], "updated_at": r["updated_at"]}
         for r in reviews),
        key=lambda x: x["project"],
    )
    text = canonical.dumps(items)
    return hashlib.sha256(((salt + ":") if salt else "").encode() + text.encode()).hexdigest()


def review_salt(conn: sqlite3.Connection, event_id: str, judge_id: str) -> str | None:
    row = conn.execute("SELECT salt FROM review_salts WHERE event_id = ? AND judge_id = ?", (event_id, judge_id)).fetchone()
    return row["salt"] if row else None


def review_digest_for(conn: sqlite3.Connection, event_id: str, judge_id: str) -> str:
    return _review_digest(domain.reviews_for(conn, event_id=event_id, judge_id=judge_id),
                          review_salt(conn, event_id, judge_id))


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
                    base_url: str, awards: dict[str, str] | None = None,
                    reasons: dict[str, str] | None = None, groups_reason: str = "") -> dict:
    """Sign and freeze the results. awards maps prize id to project id; a
    prize left out gets the suggested default, and an empty string means
    "not awarded". reasons maps prize id to the organizers' reason for the
    award, required for a close call (see domain.award_check) and signed
    into the record with it. groups_reason is required when the judges fall
    into groups that share no projects: judge lean cannot be compared
    across them, so one ranking of everyone is the organizers' call, and the
    record says so."""
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
        groups_reason = domain.clean_text(groups_reason, "reason for ranking across judge groups", max_len=500)
        if fit.components > 1 and not groups_reason:
            raise domain.Invalid(
                f"the judges fall into {fit.components} groups that share no projects, so judge lean cannot be "
                "compared across them and one ranking of everyone is not something the reviews can support on "
                "their own; add bridging assignments, or give a reason for ranking across them, which is signed "
                "into the record")
        ranked = {p.project for p in fit.projects}
        chosen, close_calls = [], []
        overall_taken: set[str] = set()
        for prize in domain.prizes(conn, event_id):
            group = domain.award_groups(fit, res["projects"], prize, overall_taken)
            top = group[0] if group else None
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
            if not prize["track_id"]:
                overall_taken.add(pick)
            check = domain.award_check(fit, pick, group)
            reason = domain.clean_text((reasons or {}).get(prize["id"], ""), f"reason for {prize['name']}", max_len=500)
            if check["close_call"] and not reason:
                close_calls.append(prize["name"])
            chosen.append({"prize": prize["id"], "name": prize["name"], "track": prize["track_id"], "project": pick,
                           "title": res["projects"][pick]["title"], "team": res["projects"][pick]["team_id"],
                           "rank": fit.project(pick).rank, "rival": check["rival"],
                           "p_beats_rival": None if check["p"] is None else round(check["p"], 4),
                           "close_call": check["close_call"], "reason": reason or None, "suggested": top, "overrode_suggestion": pick != top})
        if close_calls:
            raise domain.Invalid(
                f"{', '.join(close_calls)}: {'this is a close call' if len(close_calls) == 1 else 'these are close calls'} "
                "(under 60% sure the winner truly beats the best other candidate); "
                "give a reason for each, and it is signed into the record")
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
                "intervals": "90%, Student's t with the residual degrees of freedom",
                "probabilities": "averaged over the likelihood of the judge-to-noise ratio and of the spread "
                                 "of true quality; Student's t for the review noise; not stated below "
                                 f"{normalize.MIN_DOF} residual degrees of freedom",
                "judge_sd": round(fit.judge_sd, 4), "noise_sd": round(fit.noise_sd, 4),
                "spread_sd": round(fit.spread_sd, 4), "spread": fit.spread_summary, "dof": fit.dof,
                "observations": fit.n_observations, "graph_components": fit.components,
                "across_groups_reason": groups_reason or None,
                "registered": {"sha256": event["method_hash"], "at": event["rubric_locked_at"]}
                if event["method_hash"] else None,
                "sha256": method.digest(method.describe(res["criteria"])),
                "matches_registered": None if not event["method_hash"]
                else method.digest(method.describe(res["criteria"])) == event["method_hash"],
                "description": method.describe(res["criteria"]),
            },
            "rubric": [{"key": c["key"], "weight": c["weight"], "min": c["min_value"], "max": c["max_value"]}
                       for c in res["criteria"]],
            "rubric_history": domain.rubric_history(conn, event_id),
            "awards": chosen,
            "ranking": ranking,
            "community_vote": domain.tally_summary(conn, event_id),
            "excluded_reviews": len(res["excluded"]),
            "extensions": [{"team": x["team_id"], "until": x["until"], "reason": x["reason"]}
                           for x in domain.extensions(conn, event_id)],
            "audit_head": head,
        }, base_url)

        judge_records = 0
        for j in domain.judges(conn, event_id):
            reviews = domain.reviews_for(conn, event_id=event_id, judge_id=j["id"])
            if not reviews:
                continue
            salt = secrets.token_hex(16)
            conn.execute("INSERT INTO review_salts(event_id, judge_id, salt) VALUES (?,?,?)", (event_id, j["id"], salt))
            _issue(conn, signer, event_id, "judge", j["id"], {
                "event": ev,
                "judge": {"id": j["id"], "name": j["name"]},
                "reviews": len(reviews),
                "review_digest": _review_digest(reviews, salt),
                "review_digest_method": "sha256 of '<salt>:' + canonical JSON of the reviews; the salt is in the "
                                        "judge's own score export",
                "results_record": results_id,
                "audit_head": head,
            }, base_url)
            judge_records += 1

        by_project = {p.project: p for p in fit.projects}
        team_records = 0
        for t in conn.execute("SELECT * FROM teams WHERE event_id = ? ORDER BY id", (event_id,)).fetchall():
            # A team whose entry was disqualified gets no certificate: one
            # would read as a clean participation it was not.
            proj = conn.execute(
                f"SELECT p.* FROM projects p WHERE p.team_id = ? AND {domain.live_entry('p')}", (t["id"],)
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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _issue_receipt(conn: sqlite3.Connection, signer: Signer, base_url: str, event_id: str, kind: str, subject: str,
                   content: dict, *, owner_id: str | None = None, team_id: str | None = None) -> sqlite3.Row:
    """The receipt for exactly this content: the one already issued, or a
    new one with the next number. Issued when the receipt page is first
    opened, so a crash between submitting and receipting loses nothing."""
    digest = _sha(canonical.dumps(content))
    with transaction(conn):
        row = conn.execute("SELECT * FROM receipts WHERE kind = ? AND subject = ? AND content_sha256 = ?",
                           (kind, subject, digest)).fetchone()
        if row:
            return row
        number = conn.execute("SELECT coalesce(max(number), 0) + 1 FROM receipts").fetchone()[0]
        issued = now()
        payload = canonical.dumps({"type": f"plumb.{kind}-receipt/v1", "number": number, "issuer": base_url or None,
                                   "issued_at": issued, "content_sha256": digest, **content})
        conn.execute("INSERT INTO receipts(number, event_id, kind, subject, owner_id, team_id, content_sha256, payload, "
                     "signature, key_id, issued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (number, event_id, kind, subject, owner_id, team_id, digest, payload, signer.sign(payload),
                      signer.key_id, issued))
        audit.append(conn, f"receipt.{kind}", actor_id=owner_id, event_id=event_id, subject=subject,
                     detail={"number": number, "content_sha256": digest})
        return conn.execute("SELECT * FROM receipts WHERE number = ?", (number,)).fetchone()


def review_receipt(conn: sqlite3.Connection, actor: domain.Actor | None, project_id: str, signer: Signer,
                   base_url: str) -> sqlite3.Row:
    """A judge's receipt for their submitted review of project_id, as it
    stands. The comment is covered by its hash, so the receipt can be shown
    without repeating it."""
    actor = domain.require_user(actor)
    review = domain.own_review(conn, actor, project_id)
    if review is None:
        raise domain.NotFound("you have not submitted a review of this project")
    project = domain.get_project(conn, project_id)
    content = {"event": project["event_id"], "project": {"id": project["id"], "title": project["title"]},
               "judge": actor.id, "review": review["id"], "values": review["values"],
               "comment_sha256": _sha(review["comment"]), "submitted_at": review["submitted_at"],
               "updated_at": review["updated_at"]}
    return _issue_receipt(conn, signer, base_url, project["event_id"], "review", review["id"], content, owner_id=actor.id)


def entry_receipt(conn: sqlite3.Connection, actor: domain.Actor | None, project_id: str, signer: Signer,
                  base_url: str) -> sqlite3.Row:
    """A team's receipt for its submitted entry, as it stands: what was
    submitted and when, with the long fields covered by their hashes."""
    actor = domain.require_user(actor)
    project = domain.get_project(conn, project_id)
    if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                        (project["team_id"], actor.id)).fetchone():
        raise domain.Forbidden("that is not your team's entry")
    if project["status"] != "submitted":
        raise domain.Conflict("this entry is a draft; submit it to get a receipt")
    content = {"event": project["event_id"], "team": project["team_id"],
               "project": {"id": project["id"], "title": project["title"], "track": project["track_id"],
                           "repo_url": project["repo_url"], "demo_url": project["demo_url"],
                           "summary_sha256": _sha(project["summary"]), "description_sha256": _sha(project["description"])},
               "submitted_at": project["submitted_at"], "updated_at": project["updated_at"]}
    return _issue_receipt(conn, signer, base_url, project["event_id"], "entry", project_id, content,
                          owner_id=actor.id, team_id=project["team_id"])


def get_receipt(conn: sqlite3.Connection, actor: domain.Actor | None, number: int) -> sqlite3.Row:
    """A receipt, for its owner (the judge, or the team) and the event's organizers."""
    actor = domain.require_user(actor)
    row = conn.execute("SELECT * FROM receipts WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise domain.NotFound("no such receipt")
    mine = (row["owner_id"] == actor.id if row["kind"] == "review" else bool(conn.execute(
        "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (row["team_id"], actor.id)).fetchone()))
    if not mine and not domain.is_organizer(conn, row["event_id"], actor):
        raise domain.NotFound("no such receipt")   # the same answer as a missing one
    return row


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
    spread = m.get("spread")
    fit = SimpleNamespace(
        projects=projects, n_observations=m["observations"], judge_sd=m["judge_sd"], noise_sd=m["noise_sd"],
        spread_sd=m.get("spread_sd", 0.0), spread_summary=spread, dof=m.get("dof"),
        # Records signed before v1.1 carry only the moment estimate of the spread.
        barely_separated=spread["zero_plausible"] if spread else m.get("spread_sd", 0.0) < m["noise_sd"] / 3,
        few_reviews=m.get("dof") is not None and m["dof"] < normalize.MIN_DOF,
        components=m["graph_components"], identifiable=True,
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
    found = signer.public_key_for(row["key_id"])
    return {"payload": row["payload"], "signature": row["signature"], "key_id": row["key_id"],
            "public_key": found[0] if found else None}


def check_envelope(env: dict, signer: Signer) -> dict:
    """Verify a pasted envelope against this portal's keys.

    Uses this portal's published keys (the current one and any it has
    retired), never a key inside the envelope: a forger can always sign with
    a key of their own.
    """
    payload, signature = env.get("payload"), env.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str):
        return {"ok": False, "reason": "the envelope needs a payload string and a signature"}
    key_id = env.get("key_id") or signer.key_id
    found = signer.public_key_for(key_id)
    if found is None:
        return {"ok": False, "reason": f"signed by key {key_id}, which this portal has never used "
                                       f"(its key is {signer.key_id})"}
    public_key, retired_at = found
    if not verify(payload, signature, public_key):
        return {"ok": False, "reason": "the signature does not match the payload; it was altered or not issued here"}
    try:
        body = json.loads(payload)
    except ValueError:
        return {"ok": False, "reason": "the payload is not JSON"}
    # Only one byte string may claim to be a record: the canonical one.
    if canonical.dumps(body) != payload:
        return {"ok": False, "reason": "the signature matches, but the payload is not in canonical form"}
    reason = "signature valid" if retired_at is None else f"signature valid, by key {key_id}, retired {retired_at}"
    return {"ok": True, "reason": reason, "body": body}
