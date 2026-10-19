"""The community vote: secret from everyone, checkable by anyone, and closed to
the people who run it."""

import hashlib
import json

from app import domain

from .conftest import client_for, post_form, signup, ts

FIX, LIVE, REPLAY = "evt_01", "evt_demo_live", "evt_replay"


def set_event(db, event_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE events SET {sets} WHERE id = ?", (*cols.values(), event_id))


def test_the_audit_log_never_says_who_voted(app, db, organizer):
    v = signup(app, "secret@example.net")
    assert v.post("/api/v1/projects/prj_03/vote").status_code == 204
    assert v.delete("/api/v1/projects/prj_03/vote").status_code == 204
    assert v.post("/api/v1/projects/prj_04/vote").status_code == 204
    me = v.get("/api/v1/me").json()["id"]
    rows = db.execute("SELECT actor_id, ip, subject, detail FROM audit_log WHERE action IN ('vote.cast', 'vote.withdrawn')"
                      ).fetchall()
    assert rows
    for actor_id, ip, subject, detail in rows:
        assert actor_id is None and ip is None and subject == "sealed"
        assert me not in detail and "prj_" not in detail
    # Not through the organizer's audit view either.
    entries = organizer.get(f"/api/v1/events/{FIX}/audit?action=vote").json()["entries"]
    assert entries and not any(me in json.dumps(e) for e in entries)


def test_anyone_can_check_the_closed_tally_against_the_sealed_log(app, db, organizer):
    voters = [signup(app, f"tally{i}@example.net") for i in range(3)]
    for v in voters:
        assert v.post("/api/v1/projects/prj_05/vote").status_code == 204
    assert voters[0].post("/api/v1/projects/prj_06/vote").status_code == 204
    assert voters[1].post("/api/v1/projects/prj_06/vote").status_code == 204
    assert voters[1].delete("/api/v1/projects/prj_06/vote").status_code == 204
    voided = voters[2].get("/api/v1/me").json()["id"]
    assert organizer.post(f"/api/v1/events/{FIX}/void-votes",
                          json={"voter_id": voided, "reason": "same person twice"}).json() == {"removed": 1}

    anon = client_for(app)
    assert anon.get(f"/api/v1/events/{FIX}/votes/log").status_code == 403   # sealed while open
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    log = anon.get(f"/api/v1/events/{FIX}/votes/log").json()
    counted = domain.counted_seals(log)
    for v in log["votes"]:
        assert hashlib.sha256(f"{v['nonce']}:{v['project']}".encode()).hexdigest() == v["seal"]
    assert {v["seal"] for v in log["votes"]} == counted
    assert {v["project"] for v in log["votes"]} == {"prj_05", "prj_06"}
    assert sorted(v["project"] for v in log["votes"]) == ["prj_05", "prj_05", "prj_06"]
    voids = [e for e in log["entries"] if e["action"] == "votes.voided"]
    assert voids[0]["detail"]["reason"] == "same person twice" and voided not in json.dumps(log)
    tallies = {t["project_id"]: t["votes"] for t in anon.get(f"/api/v1/events/{FIX}/votes").json()["tallies"]}
    assert tallies["prj_05"] == 2 and tallies["prj_06"] == 1
    page = anon.get(f"/events/{FIX}/votes").text
    assert "Check the count yourself" in page and "1 vote voided" in page


def test_the_published_record_signs_the_tally(db):
    body = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'",
                                 (REPLAY,)).fetchone()[0])
    vote = body["community_vote"]
    tallies = {r["id"]: r["votes"] for r in domain.vote_tallies(db, None, REPLAY)}
    assert {t["project"]: t["votes"] for t in vote["tally"]} == tallies
    assert vote["votes"] == sum(tallies.values()) and vote["voided"]["votes"] == 2
    seals = sorted(v["seal"] for v in domain.vote_reveal(db, None, REPLAY)["votes"])
    assert vote["counted_seals_sha256"] == hashlib.sha256(" ".join(seals).encode()).hexdigest()


def test_votes_cannot_be_voided_after_publication(organizer, db):
    voter = db.execute("SELECT voter_id FROM votes WHERE event_id = ? LIMIT 1", (REPLAY,)).fetchone()[0]
    before = domain.turnout(db, REPLAY)
    r = organizer.post(f"/api/v1/events/{REPLAY}/void-votes", json={"voter_id": voter, "reason": "after the fact"})
    assert r.status_code == 409
    assert domain.turnout(db, REPLAY) == before


def test_the_replay_vote_was_cast_through_the_real_ballot(db):
    # Every counted replay vote has its sealed vote.cast entry; nothing was written around the log.
    log = domain.vote_log(db, REPLAY)
    assert len(log["votes"]) == db.execute("SELECT COUNT(*) FROM votes WHERE event_id = ?", (REPLAY,)).fetchone()[0]
    assert {v["seal"] for v in log["votes"]} == domain.counted_seals(log)
    assert not db.execute("SELECT 1 FROM audit_log WHERE action = 'demo.votes_seeded'").fetchone()


def test_people_who_run_the_event_do_not_vote_or_compete(app, organizer, judge_a, db):
    admin = client_for(app, "admin")
    for who in (organizer, admin, judge_a):
        assert who.post("/api/v1/projects/prj_02/vote").status_code == 403
    page = organizer.get(f"/events/{FIX}").text
    assert "Open your ballot" not in page and "do not vote in it" in page
    assert "Open your ballot" not in judge_a.get(f"/events/{FIX}").text
    assert admin.post(f"/api/v1/events/{LIVE}/teams", json={"name": "Admins United"}).status_code == 403


def test_a_vote_cannot_open_while_teams_can_still_change(organizer, db):
    r = organizer.patch(f"/api/v1/events/{LIVE}", json={"voting_mode": "accounts", "voting_open": ts(1),
                                                       "voting_close": ts(24 * 30)})
    assert r.status_code == 422 and "submissions have closed" in r.json()["error"]


def test_participants_only_means_a_team_with_a_submitted_entry(app, db):
    def team(email, name, submit):
        c = signup(app, email)
        assert post_form(c, f"/events/{LIVE}/teams", {"name": name}).status_code == 303
        pid = None
        if submit:
            r = post_form(c, f"/events/{LIVE}/projects/new", {"title": name, "summary": "s", "action": "submit"})
            pid = r.headers["location"].split("/")[2].split("?")[0]
        return c, pid

    empty, _ = team("empty@example.net", "Nothing Yet", submit=False)
    builder, mine = team("builder@example.net", "Builders", submit=True)
    _, theirs = team("other@example.net", "Others", submit=True)
    set_event(db, LIVE, submissions_open=ts(-48), submissions_close=ts(-24), judging_close=None,
              voting_mode="participants", voting_open=ts(-1), voting_close=ts(24))
    assert empty.post(f"/api/v1/projects/{theirs}/vote").status_code == 403
    assert builder.post(f"/api/v1/projects/{mine}/vote").status_code == 403      # own team
    assert builder.post(f"/api/v1/projects/{theirs}/vote").status_code == 204


def test_the_integrity_page_words_an_open_vote_without_leaking_it(app, organizer):
    for i in range(3):
        signup(app, f"fresh-sock{i}@example.net").post("/api/v1/projects/prj_09/vote")
    page = organizer.get(f"/events/{FIX}/manage/integrity").text
    assert "of None" not in page and "named, with its total, when voting closes" in page
