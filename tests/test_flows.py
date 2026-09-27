"""End-to-end flows through the real HTTP routes, one role at a time."""

import json
import re

from app import audit, records
from app.signing import verify

from .conftest import client_for, csrf_of, post_form, signup, ts

LIVE = "evt_demo_live"
FIX = "evt_01"


def set_event(db, event_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE events SET {sets} WHERE id = ?", (*cols.values(), event_id))


# --- participants ----------------------------------------------------------------


def test_team_formation_submission_edit_and_deadline(app, db):
    alice = signup(app, "alice@example.net", "Alice")
    r = post_form(alice, f"/events/{LIVE}/teams", {"name": "Lighthouse"})
    assert r.status_code == 303
    page = alice.get(f"/events/{LIVE}/team").text
    code = re.search(r"/join/([A-Za-z0-9_-]+)", page).group(1)

    bob = signup(app, "bob@example.net", "Bob")
    assert post_form(bob, f"/join/{code}", {}).status_code == 303
    assert "Bob" in alice.get(f"/events/{LIVE}/team").text

    # Draft: visible to the team, invisible to everyone else.
    r = post_form(alice, f"/events/{LIVE}/projects/new", {"title": "Beacon", "summary": "", "action": "draft"})
    assert r.status_code == 303
    pid = r.headers["location"].split("/")[2].split("?")[0]
    assert bob.get(f"/projects/{pid}").status_code == 200
    assert client_for(app).get(f"/projects/{pid}").status_code == 404
    assert f"/projects/{pid}" not in client_for(app).get("/projects?q=Beacon").text

    # Submitting needs a summary; the form keeps what was typed.
    r = post_form(alice, f"/projects/{pid}/edit", {"title": "Beacon", "summary": "", "action": "submit"})
    assert r.status_code == 422 and 'value="Beacon"' in r.text
    r = post_form(alice, f"/projects/{pid}/edit", {"title": "Beacon", "summary": "A light for lost packets",
                                                   "action": "submit"})
    assert r.status_code == 303
    assert "Beacon" in client_for(app).get("/projects?q=packets").text

    # Edit until the deadline...
    assert post_form(bob, f"/projects/{pid}/edit", {"title": "Beacon 2", "summary": "s"}).status_code == 303
    # ...and not a second after it.
    set_event(db, LIVE, submissions_close=ts(-0.01), submissions_open=ts(-2))
    r = post_form(bob, f"/projects/{pid}/edit", {"title": "Sneaky", "summary": "s"})
    assert r.status_code == 409
    assert client_for(app).get(f"/projects/{pid}").text.count("Beacon 2") >= 1
    # Teams freeze too.
    carol = signup(app, "carol@example.net")
    assert post_form(carol, f"/join/{code}", {}).status_code == 409


def test_one_team_per_event_and_team_size(app, db):
    set_event(db, LIVE, max_team_size=2)
    a = signup(app, "a1@example.net")
    post_form(a, f"/events/{LIVE}/teams", {"name": "Solo"})
    assert post_form(a, f"/events/{LIVE}/teams", {"name": "Second"}).status_code == 409
    code = re.search(r"/join/([A-Za-z0-9_-]+)", a.get(f"/events/{LIVE}/team").text).group(1)
    post_form(signup(app, "a2@example.net"), f"/join/{code}", {})
    assert post_form(signup(app, "a3@example.net"), f"/join/{code}", {}).status_code == 409


def test_forms_require_csrf(app):
    alice = signup(app, "csrf@example.net")
    r = alice.post(f"/events/{LIVE}/teams", data={"name": "NoToken"}, follow_redirects=False)
    assert r.status_code == 403
    r = alice.post(f"/events/{LIVE}/teams", data={"name": "BadToken", "csrf": "nope"}, follow_redirects=False)
    assert r.status_code == 403
    # Cookie-authenticated JSON from another origin is refused.
    r = alice.post(f"/api/v1/events/{LIVE}/teams", json={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_hostile_inputs_are_422_not_500(app, organizer, participant):
    c = signup(app, "hostile@example.net")
    assert c.post("/api/v1/projects/prj_09/comments", json={"body": "\u0000abc"}).status_code == 422
    assert c.post("/api/v1/verify", content=b'{"payload":"x","signature":"y"}',
                  headers={"Content-Type": "text/plain"}).status_code == 422
    assert c.post(f"/api/v1/events/{LIVE}/teams", json={"name": "Hostiles"}).status_code == 201
    assert c.post(f"/events/{LIVE}/projects/new", json={"title": 123}).status_code == 422
    assert organizer.patch(f"/api/v1/events/{LIVE}", json={"reviews_per_project": 0}).status_code == 422
    assert organizer.post(f"/api/v1/events/{FIX}/duplicates", json={"keep": "prj_07", "drop": "prj_02"}).status_code == 422
    assert organizer.get("/api/v1/events/nope/records").status_code == 404


def test_signup_cannot_claim_an_imported_judge_account(app):
    c = client_for(app)
    token = csrf_of(c, "/signup")
    r = c.post("/signup", data={"csrf": token, "email": "tomas.varga@example.org", "name": "Imposter",
                                "password": "long enough password"}, follow_redirects=False)
    assert r.status_code == 409


# --- judges --------------------------------------------------------------------------


def test_invitation_creates_account_and_judge_can_review(app, organizer, db):
    r = post_form(organizer, f"/events/{FIX}/manage/invite", {"email": "newjudge@example.net", "role": "judge"},
                  csrf_from=f"/events/{FIX}/manage/judges")
    link = re.search(r'value="(http://testserver/invitations/[^"]+)"', r.text).group(1)
    path = link.replace("http://testserver", "")
    j = client_for(app)
    r = j.post(path, data={"csrf": csrf_of(j, path), "name": "New Judge", "password": "a fine password"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "judge" in j.get("/me").text
    # The link is single use.
    other = client_for(app)
    r = other.post(path, data={"csrf": csrf_of(other, "/login"), "name": "Again", "password": "another password"})
    assert r.status_code == 409

    # Assign one project and review it.
    uid = db.execute("SELECT id FROM users WHERE email = 'newjudge@example.net'").fetchone()["id"]
    post_form(organizer, f"/events/{FIX}/manage/assignments/add", {"judge_id": uid, "project_id": "prj_01"},
              csrf_from=f"/events/{FIX}/manage/judges")
    form = {"functionality": "5", "quality": "4", "innovation": "3", "comment": "Tidy."}
    assert post_form(j, "/projects/prj_01/review", form, csrf_from=f"/events/{FIX}/judge").status_code == 303
    mine = j.get("/api/judge/scores").json()["reviews"]
    assert [r["values"] for r in mine] == [{"functionality": 5, "quality": 4, "innovation": 3}]
    # Out of range and unassigned are refused.
    bad = {**form, "quality": "9"}
    assert post_form(j, "/projects/prj_01/review", bad, csrf_from=f"/events/{FIX}/judge").status_code == 422
    assert post_form(j, "/projects/prj_02/review", form, csrf_from=f"/events/{FIX}/judge").status_code == 403


def test_judge_isolation_everywhere(judge_a, judge_b, db):
    ja = db.execute("SELECT id FROM users WHERE email = (SELECT email FROM users WHERE id='jdg_24')").fetchone()["id"]
    assert judge_b.get(f"/api/judge/scores?judge={ja}").status_code == 403
    assert judge_b.get(f"/api/judge/scores?judge={ja}&event={FIX}").status_code == 403
    assert judge_b.get(f"/api/v1/events/{FIX}/results").status_code == 403
    assert judge_b.get(f"/events/{FIX}/manage/reviews?judge={ja}").status_code == 403
    assert judge_b.get(f"/events/{FIX}/export/scores.csv").status_code == 403
    # A judge cannot review a project assigned to someone else.
    other = db.execute(
        "SELECT project_id FROM assignments WHERE judge_id = 'jdg_24' AND project_id NOT IN "
        "(SELECT project_id FROM assignments WHERE judge_id = 'jdg_26') LIMIT 1").fetchone()[0]
    r = judge_b.put(f"/api/v1/projects/{other}/review", json={"values": {"functionality": 1, "quality": 1, "innovation": 1}})
    assert r.status_code == 403


def test_auto_assignment_reaches_target_without_conflicts(organizer, db):
    r = post_form(organizer, f"/events/{FIX}/manage/assignments/auto", {}, csrf_from=f"/events/{FIX}/manage")
    assert r.status_code == 303
    counts = db.execute(
        "SELECT p.id, COUNT(a.judge_id) AS n FROM projects p LEFT JOIN assignments a ON a.project_id = p.id "
        "WHERE p.event_id = ? AND p.duplicate_of IS NULL GROUP BY p.id", (FIX,)
    ).fetchall()
    assert all(c["n"] >= 3 for c in counts)
    wrong_track = db.execute(
        "SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id "
        "WHERE a.source = 'auto' AND NOT EXISTS (SELECT 1 FROM judge_tracks jt WHERE jt.user_id = a.judge_id "
        "AND jt.track_id = p.track_id)"
    ).fetchone()[0]
    assert wrong_track == 0


# --- organizers: rubric, duplicates, publication, records -----------------------------------


def test_duplicate_is_flagged_on_import_and_can_be_swapped(organizer, db, anon):
    # The team's latest submission is its entry; the earlier one is the duplicate.
    assert db.execute("SELECT duplicate_of FROM projects WHERE id = 'prj_07'").fetchone()[0] == "prj_41"
    assert "Dry Harbour" in organizer.get(f"/events/{FIX}/manage/integrity").text
    r = post_form(organizer, f"/events/{FIX}/manage/duplicates", {"keep": "prj_07", "drop": "prj_41"},
                  csrf_from=f"/events/{FIX}/manage")
    assert r.status_code == 303
    assert db.execute("SELECT duplicate_of FROM projects WHERE id = 'prj_41'").fetchone()[0] == "prj_07"
    assert db.execute("SELECT duplicate_of FROM projects WHERE id = 'prj_07'").fetchone()[0] is None
    assert anon.get("/projects/prj_41").status_code == 404
    assert anon.get("/projects/prj_07").status_code == 200


def test_track_prizes_from_the_settings_form(organizer, db):
    form = {k: db.execute(f"SELECT {k} FROM events WHERE id = 'evt_demo_live'").fetchone()[0] or ""
            for k in ("name", "submissions_open", "submissions_close", "judging_close")}
    r = post_form(organizer, f"/events/{LIVE}/manage/settings",
                  {**form, "prizes": "Best in show\nBest game | most fun | Games"}, csrf_from=f"/events/{LIVE}/manage/settings")
    assert r.status_code == 303
    rows = db.execute("SELECT p.name, t.name AS track FROM prizes p LEFT JOIN tracks t ON t.id = p.track_id "
                      "WHERE p.event_id = ? ORDER BY p.position", (LIVE,)).fetchall()
    assert [(r["name"], r["track"]) for r in rows] == [("Best in show", None), ("Best game", "Games")]
    assert "Best game | most fun | Games" in organizer.get(f"/events/{LIVE}/manage/settings").text
    r = post_form(organizer, f"/events/{LIVE}/manage/settings",
                  {**form, "prizes": "Best robot | | Robots"}, csrf_from=f"/events/{LIVE}/manage/settings")
    assert r.status_code == 422


def test_rubric_weights_change_the_live_ranking(organizer):
    before = organizer.get(f"/api/v1/events/{FIX}/results").json()["ranking"]
    r = organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"functionality": 0, "quality": 0, "innovation": 1}})
    assert r.status_code == 200
    after = organizer.get(f"/api/v1/events/{FIX}/results").json()["ranking"]
    assert [p["project_id"] for p in before] != [p["project_id"] for p in after]
    r = organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"functionality": 0, "quality": 0, "innovation": 0}})
    assert r.status_code == 422


def test_publish_issues_signed_records_and_locks_reviews(app, organizer, judge_a, anon, db):
    assert "not published yet" in anon.get(f"/events/{FIX}/results").text
    # Not while the community vote is open.
    assert organizer.post(f"/api/v1/events/{FIX}/publish").status_code == 409
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    r = post_form(organizer, f"/events/{FIX}/manage/publish", {}, csrf_from=f"/events/{FIX}/manage/results")
    assert r.status_code == 303 and anon.get(f"/events/{FIX}/results").status_code == 200
    assert "not published yet" in anon.get(f"/events/{FIX}/results").text  # no confirm tick, nothing happened
    r = post_form(organizer, f"/events/{FIX}/manage/publish", {"confirm": "1"}, csrf_from=f"/events/{FIX}/manage/results")
    assert r.status_code == 303
    assert anon.get(f"/events/{FIX}/results").status_code == 200
    kinds = [row["kind"] for row in db.execute("SELECT kind FROM records WHERE event_id = ?", (FIX,))]
    assert kinds.count("results") == 1 and kinds.count("judge") == 30 and kinds.count("team") == 40

    signer = app.state.signer
    rec = db.execute("SELECT * FROM records WHERE event_id = ? AND kind = 'judge' AND subject = 'jdg_24'", (FIX,)).fetchone()
    env = anon.get(f"/records/{rec['id']}.json").json()
    assert verify(env["payload"], env["signature"], signer.public_key_b64)
    body = json.loads(env["payload"])
    assert body["reviews"] == 11
    # The judge can recompute their digest from their own export.
    assert body["review_digest"] == records.review_digest_for(db, FIX, "jdg_24")
    # Tampering is caught.
    forged = {**env, "payload": env["payload"].replace('"reviews":11', '"reviews":12')}
    assert anon.post("/api/v1/verify", json=forged).json()["ok"] is False
    assert anon.post("/api/v1/verify", json=env).json()["ok"] is True

    # The public page shows exactly the signed ranking.
    signed = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'", (FIX,)).fetchone()[0])
    page = anon.get(f"/events/{FIX}/results").text
    assert signed["ranking"][0]["title"] in page

    # Nothing that would make the live event disagree with the record is allowed.
    top = signed["ranking"][0]["project"]
    assert organizer.post(f"/api/v1/projects/{top}/disqualify", json={"reason": "late"}).status_code == 409
    assert organizer.patch(f"/api/v1/events/{FIX}", json={"submissions_close": ts(24 * 365)}).status_code == 409
    assert organizer.post(f"/api/v1/events/{FIX}/duplicates", json={"keep": "prj_07", "drop": "prj_41"}).status_code == 409

    # Reviews and the rubric are locked now.
    r = judge_a.put("/api/v1/projects/prj_06/review", json={"values": {"functionality": 1, "quality": 1, "innovation": 1}})
    assert r.status_code == 409
    assert organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"quality": 2}}).status_code == 409
    # Certificates render.
    team = db.execute("SELECT id FROM records WHERE event_id = ? AND kind = 'team' LIMIT 1", (FIX,)).fetchone()["id"]
    assert "CERTIFICATE" in anon.get(f"/records/{team}").text


# --- voting and comments --------------------------------------------------------------------------


def test_ballot_is_shuffled_per_voter_and_counts_stay_hidden(app, participant, organizer, db):
    v1, v2 = signup(app, "voter1@example.net"), signup(app, "voter2@example.net")
    order1 = [p["id"] for p in v1.get(f"/api/v1/events/{FIX}/ballot").json()["projects"]]
    order2 = [p["id"] for p in v2.get(f"/api/v1/events/{FIX}/ballot").json()["projects"]]
    assert sorted(order1) == sorted(order2) and order1 != order2
    assert order1 == [p["id"] for p in v1.get(f"/api/v1/events/{FIX}/ballot").json()["projects"]]
    assert "prj_07" not in order1  # duplicates are not on the ballot

    assert v1.post("/api/v1/projects/prj_02/vote").status_code == 204
    assert v1.post("/api/v1/projects/prj_02/vote").status_code == 409  # once per project
    for p in ("prj_03", "prj_04"):
        assert v1.post(f"/api/v1/projects/{p}/vote").status_code == 204
    assert v1.post("/api/v1/projects/prj_05/vote").status_code == 409  # 3 votes each
    assert participant.post("/api/v1/projects/prj_01/vote").status_code == 403  # own team

    # Hidden from everyone, organizers included, until voting closes, and
    # a closed vote cannot be reopened to peek and vote again.
    assert organizer.get(f"/api/v1/events/{FIX}/votes").status_code == 403
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    tallies = organizer.get(f"/api/v1/events/{FIX}/votes").json()
    assert tallies["turnout"] == {"votes": 3, "voters": 1}
    assert v2.post("/api/v1/projects/prj_02/vote").status_code == 409  # closed
    assert organizer.patch(f"/api/v1/events/{FIX}", json={"voting_close": ts(24)}).status_code == 409


def test_judges_do_not_vote(judge_a):
    assert judge_a.post("/api/v1/projects/prj_02/vote").status_code == 403


def test_vote_flood_is_rate_limited(app):
    v = signup(app, "flood@example.net")
    codes = [v.delete("/api/v1/projects/prj_02/vote").status_code for _ in range(35)]
    assert 429 in codes


def test_new_accounts_voting_show_up_as_a_signal_and_can_be_voided(app, organizer):
    for i in range(3):
        signup(app, f"sock{i}@example.net").post("/api/v1/projects/prj_09/vote")
    signals = organizer.get(f"/api/v1/events/{FIX}/abuse").json()
    assert len(signals["fresh_accounts"]) == 3
    # While voting is open the project is not named: that would leak the race.
    assert signals["concentrated"][0]["project_id"] is None
    assert signals["concentrated"][0]["from_new_accounts"] == 3
    voter = signals["fresh_accounts"][0]["id"]
    r = organizer.post(f"/api/v1/events/{FIX}/void-votes", json={"voter_id": voter, "reason": "sock puppet"})
    assert r.json() == {"removed": 1}


def test_comments_and_moderation(app, organizer, anon):
    c = signup(app, "commenter@example.net", "Commenter")
    cid = c.post("/api/v1/projects/prj_03/comments", json={"body": "Love the demo"}).json()["id"]
    assert "Love the demo" in anon.get("/projects/prj_03").text
    assert organizer.post(f"/api/v1/comments/{cid}/hide", json={"reason": "test"}).status_code == 204
    assert "Love the demo" not in anon.get("/projects/prj_03").text
    assert c.post(f"/api/v1/comments/{cid}/hide", json={"reason": "mine"}).status_code == 403


# --- audit trail ---------------------------------------------------------------------------------------


def test_every_action_is_audited_and_the_chain_detects_tampering(organizer, db):
    organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"quality": 2}})
    actions = [r["action"] for r in organizer.get(f"/api/v1/events/{FIX}/audit").json()["entries"]]
    assert "rubric.updated" in actions and "event.imported" in actions
    assert audit.verify_chain(db)["ok"]
    # The database refuses edits outright...
    import sqlite3
    try:
        db.execute("UPDATE audit_log SET action = 'x' WHERE seq = 2")
        raise AssertionError("update should have been refused")
    except sqlite3.IntegrityError:
        pass
    # ...and if someone drops the trigger to force one, the chain shows it.
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit_log SET detail = '{}' WHERE seq = 2")
    result = audit.verify_chain(db)
    assert result == {"ok": False, "checked": 1, "broken_at": 2}


# --- import and export ------------------------------------------------------------------------------------


def test_export_then_import_round_trips(app, organizer, db):
    doc = organizer.get(f"/events/{FIX}/export/event.json").json()
    admin = client_for(app)
    admin.post("/login", data={"csrf": csrf_of(admin, "/login"), "email": "admin@example.org",
                               "password": "plumb-demo-2026", "next": "/"})
    doc["event"]["id"] = "evt_copy"
    r = admin.post("/api/v1/events/import", json=doc)
    assert r.status_code == 201, r.text
    again = organizer.get("/events/evt_copy/export/event.json")
    assert again.status_code == 403  # the organizer of the original is not the organizer of the copy
    copy = admin.get("/events/evt_copy/export/event.json").json()
    for key in ("tracks", "judges", "teams", "projects"):
        assert len(copy[key]) == len(doc[key])
    def strip(d):
        key = {p["id"]: (p["title"], p["submitted_at"]) for p in d["projects"]}
        return sorted((x["judge"], key[x["project"]], json.dumps(x["criteria"], sort_keys=True)) for x in d["scores"])

    assert strip(copy) == strip(doc)
    assert len(copy["plumb"]["duplicates"]) == 1
    assert r.json()["ids_renamed"] == 8 + 40 + 41  # every track, team and project id was taken


def test_organizer_of_one_event_cannot_hijack_another_events_judge(app, organizer):
    # organizer@example.org organizes evt_demo_live; tomas.varga is an unclaimed judge of evt_01.
    r = organizer.post(f"/api/v1/events/{LIVE}/invitations", json={"email": "tomas.varga@example.org"})
    path = r.json()["link"].split("testserver")[1]
    anon = client_for(app)
    page = anon.get(path).text
    assert "already belongs to another event" in page
    r = anon.post(path, data={"csrf": csrf_of(anon, "/login"), "name": "Mallory", "password": "long enough pw"},
                  follow_redirects=False)
    assert r.status_code == 403
    assert "plumb_session" not in r.cookies


def test_spoofed_forwarded_for_does_not_reset_login_limits(app):
    c = client_for(app)
    codes = []
    for i in range(12):
        codes.append(c.post("/login", data={"csrf": csrf_of(c, "/login"), "email": "organizer@example.org",
                                            "password": "wrong", "next": "/"},
                            headers={"X-Forwarded-For": f"10.9.8.{i}"}).status_code)
    assert codes[-1] == 429


def test_import_rejects_broken_files_atomically(app, db):
    admin = client_for(app)
    admin.post("/login", data={"csrf": csrf_of(admin, "/login"), "email": "admin@example.org",
                               "password": "plumb-demo-2026", "next": "/"})
    bad = {"event": {"id": "evt_bad", "name": "Bad", "submissions_close": "2026-01-01T00:00:00Z"},
           "tracks": [], "judges": [], "teams": [],
           "projects": [{"id": "p1", "team": "nope", "title": "x"}], "scores": []}
    r = admin.post("/api/v1/events/import", json=bad)
    assert r.status_code == 422 and "unknown team" in r.json()["error"]
    assert db.execute("SELECT 1 FROM events WHERE id = 'evt_bad'").fetchone() is None


# --- webhooks -------------------------------------------------------------------------------------------------


def test_webhooks_refuse_internal_destinations(organizer):
    for url in ("http://127.0.0.1:8080/x", "http://169.254.169.254/latest", "http://10.0.0.5/hook"):
        r = organizer.post(f"/api/v1/events/{FIX}/webhooks", json={"url": url})
        assert r.status_code == 422, url


def test_webhooks_stream_the_audit_log_in_order_with_signatures(organizer, settings, monkeypatch):
    from app.routes import api as api_routes
    from app.webhooks import Deliverer, sign

    monkeypatch.setattr(api_routes, "check_destination", lambda url: None)  # hooks.example does not resolve

    hook = organizer.post(f"/api/v1/events/{FIX}/webhooks", json={"url": "https://hooks.example/plumb"}).json()
    organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"quality": 3}})
    got = []

    def fake_send(url, body, headers):
        got.append((url, body, headers))
        return 200

    d = Deliverer(settings.database_path, send=fake_send)
    assert d.run_once() >= 1
    actions = [json.loads(b)["action"] for _, b, _ in got]
    assert actions == ["webhook.created", "rubric.updated"]  # the stream starts at the hook's own creation
    url, body, headers = got[-1]
    assert headers["X-Plumb-Signature"] == sign(hook["secret"], body)
    assert d.run_once() == 0  # nothing new, nothing resent

    # A failing receiver holds the cursor; nothing is skipped.
    organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"quality": 4}})
    failing = Deliverer(settings.database_path, send=lambda *a: 500)
    assert failing.run_once() == 0
    assert Deliverer(settings.database_path, send=fake_send).run_once() == 1
