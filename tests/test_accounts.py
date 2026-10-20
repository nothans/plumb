"""Accounts: one password rule everywhere, claim links for imported people,
and the organizer's tools for invitations and judges."""

import dataclasses
import io
import re

import pytest

from app import cli
from app.security import MIN_PASSWORD, password_problem

from .conftest import client_for, csrf_of, post_form

FIX = "evt_01"
TOMAS = "tomas.varga@example.org"   # judged evt_01 and the replay, both run by organizer@example.org; never set a password


@pytest.mark.parametrize("password, ok", [
    ("a" * 14 + "b", True), ("fourteen chars", False), ("x" * 257, False), ("y" * 256, False),
    ("my plumb password", False), ("tomas.varga rocks hard", False), ("three plain words", True),
])
def test_one_password_rule(password, ok):
    assert MIN_PASSWORD == 15
    assert (password_problem(password, TOMAS) is None) == ok


def test_signup_refuses_a_short_password_and_says_why(app):
    c = client_for(app)
    r = c.post("/signup", data={"csrf": csrf_of(c, "/signup"), "email": "short@example.net", "name": "Short",
                                "password": "fourteen chars", "next": "/"}, follow_redirects=False)
    assert r.status_code == 422 and "at least 15 characters" in r.text
    assert 'minlength="15"' in c.get("/signup").text


def test_a_weak_admin_password_stops_the_boot(tmp_path, settings):
    s = dataclasses.replace(settings, data_dir=tmp_path / "prod", demo=False, admin_email="owner@example.org",
                            admin_password="short one")
    with pytest.raises(SystemExit, match="PLUMB_ADMIN_PASSWORD refused"):
        cli.bootstrap(s, out=io.StringIO())


def _claim_link(organizer, db, email=TOMAS):
    uid = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    r = post_form(organizer, f"/events/{FIX}/manage/claim-link", {"user_id": uid}, csrf_from=f"/events/{FIX}/manage/judges")
    m = re.search(r'value="http://testserver(/claim/[^"]+)"', r.text)
    return r, (m.group(1) if m else None), uid


def test_an_imported_judge_claims_their_account_with_a_link(app, organizer, db):
    page = organizer.get(f"/events/{FIX}/manage/judges").text
    assert "No account yet" in page and TOMAS in page.split("No account yet")[1]
    r, path, uid = _claim_link(organizer, db)
    assert r.status_code == 200 and path
    anon = client_for(app)
    assert TOMAS in anon.get(path).text
    r = anon.post(path, data={"csrf": csrf_of(anon, path), "name": "Tomas Varga", "password": "too short"},
                  follow_redirects=False)
    assert r.status_code == 422 and "at least 15 characters" in r.text
    r = anon.post(path, data={"csrf": csrf_of(anon, path), "name": "Tomas Varga", "password": "a long fine password"},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me"
    assert "Tomas Varga" in anon.get("/me").text
    # Single use, and the account has left the list.
    again = client_for(app)
    assert again.get(path).status_code == 409
    assert TOMAS not in organizer.get(f"/events/{FIX}/manage/judges").text.split("No account yet")[-1]
    entry = db.execute("SELECT detail FROM audit_log WHERE action = 'user.claimed' AND subject = ?", (uid,)).fetchone()
    assert entry and "claim_link" in entry["detail"]


def test_only_an_organizer_of_every_event_of_the_account_can_make_its_link(app, db):
    # rival runs evt_01 too, but not the replay, where Tomas also judged.
    from .conftest import signup
    rival = signup(app, "rival@example.org", "Rival Organizer", "a long other password")
    db.execute("INSERT INTO event_roles(event_id, user_id, role) SELECT ?, id, 'organizer' FROM users WHERE email = ?",
               (FIX, "rival@example.org"))
    db.commit()
    r, path, _ = _claim_link(rival, db)
    assert r.status_code == 403 and path is None and "an event you do not organize" in r.text


def test_a_claim_link_expires(app, organizer, db):
    _, path, _ = _claim_link(organizer, db)
    db.execute("UPDATE claim_links SET created_at = '2020-01-01T00:00:00Z'")
    db.commit()
    r = client_for(app).get(path)
    assert r.status_code == 409 and "expired after 14 days" in r.text


def test_a_revoked_invitation_link_stops_working(app, organizer):
    r = post_form(organizer, f"/events/{FIX}/manage/invite", {"email": "maybe@example.net", "role": "judge"},
                  csrf_from=f"/events/{FIX}/manage/judges")
    path = re.search(r'value="http://testserver(/invitations/[^"]+)"', r.text).group(1)
    assert client_for(app).get(path).status_code == 200
    r = post_form(organizer, f"/events/{FIX}/manage/invitations/revoke", {"email": "maybe@example.net", "role": "judge"},
                  csrf_from=f"/events/{FIX}/manage/judges")
    assert r.status_code == 303
    assert client_for(app).get(path).status_code == 404
    assert "maybe@example.net" not in organizer.get(f"/events/{FIX}/manage/judges").text


def test_a_judge_without_reviews_can_be_removed_and_one_with_reviews_cannot(app, organizer, db):
    r = post_form(organizer, f"/events/{FIX}/manage/invite", {"email": "spare@example.net", "role": "judge"},
                  csrf_from=f"/events/{FIX}/manage/judges")
    path = re.search(r'value="http://testserver(/invitations/[^"]+)"', r.text).group(1)
    j = client_for(app)
    j.post(path, data={"csrf": csrf_of(j, path), "name": "Spare Judge", "password": "an extra long password"})
    spare = db.execute("SELECT id FROM users WHERE email = 'spare@example.net'").fetchone()["id"]
    post_form(organizer, f"/events/{FIX}/manage/assignments/add", {"judge_id": spare, "project_id": "prj_01"},
              csrf_from=f"/events/{FIX}/manage/judges")
    r = post_form(organizer, f"/events/{FIX}/manage/judges/{spare}/remove", {}, csrf_from=f"/events/{FIX}/manage/judges")
    assert r.status_code == 303
    assert not db.execute("SELECT 1 FROM event_roles WHERE user_id = ? AND event_id = ?", (spare, FIX)).fetchone()
    assert not db.execute("SELECT 1 FROM assignments WHERE judge_id = ?", (spare,)).fetchone()
    busy = db.execute("SELECT judge_id FROM reviews WHERE event_id = ? LIMIT 1", (FIX,)).fetchone()[0]
    r = post_form(organizer, f"/events/{FIX}/manage/judges/{busy}/remove", {}, csrf_from=f"/events/{FIX}/manage/judges")
    assert r.status_code == 409
    assert db.execute("SELECT 1 FROM event_roles WHERE user_id = ? AND event_id = ?", (busy, FIX)).fetchone()


# --- review nits: rubric weights, form fields, exports ----------------------------------------


def _add_criterion(organizer, db, name, weight):
    db.execute("DELETE FROM reviews WHERE event_id = ?", (FIX,))   # criteria can be added only before the first review
    db.commit()
    return post_form(organizer, f"/events/{FIX}/manage/rubric", {"new_name": name, "new_weight": weight},
                     csrf_from=f"/events/{FIX}/manage/rubric")


@pytest.mark.parametrize("weight", ["abc", "inf", "nan", "-1", "101"])
def test_a_new_criterion_weight_is_validated(organizer, db, weight):
    r = _add_criterion(organizer, db, "Polish", weight)
    assert r.status_code == 422 and "weights must be" in r.text.lower()
    assert not db.execute("SELECT 1 FROM criteria WHERE event_id = ? AND key = 'polish'", (FIX,)).fetchone()


def test_a_criterion_named_comment_does_not_break_the_review_form(app, organizer, judge_a, db):
    assert _add_criterion(organizer, db, "Comment", "1").status_code == 303
    me = judge_a.get("/api/judge/scores").json()
    project = db.execute("SELECT project_id FROM assignments WHERE event_id = ? AND judge_id = ? LIMIT 1",
                         (FIX, me["judge"])).fetchone()[0]
    page = judge_a.get(f"/projects/{project}/review").text
    assert 'name="score_comment"' in page and page.count('name="comment"') == 1
    form = {"score_functionality": "4", "score_quality": "4", "score_innovation": "4", "score_comment": "5",
            "comment": "Clear write-up."}
    assert post_form(judge_a, f"/projects/{project}/review", form, csrf_from=f"/events/{FIX}/judge").status_code == 303
    mine = [x for x in judge_a.get("/api/judge/scores").json()["reviews"] if x["project_id"] == project][0]
    assert mine["values"]["comment"] == 5 and mine["comment"] == "Clear write-up."


def test_scores_csv_of_a_missing_event_is_404(app):
    from .conftest import client_for
    admin = client_for(app)
    admin.post("/login", data={"csrf": csrf_of(admin, "/login"), "email": "admin@example.org",
                               "password": "plumb-demo-2026", "next": "/"})
    assert admin.get("/events/evt_nope/export/scores.csv").status_code == 404


def test_an_idle_judges_open_projects_can_be_released_and_reassigned(organizer, db):
    # A judge who reviewed some live projects and then stopped: take back one of their reviews.
    judge, project = db.execute(
        "SELECT v.judge_id, v.project_id FROM reviews v JOIN projects p ON p.id = v.project_id "
        "WHERE v.event_id = ? AND p.duplicate_of IS NULL ORDER BY v.judge_id, v.project_id LIMIT 1", (FIX,)).fetchone()
    db.execute("DELETE FROM reviews WHERE judge_id = ? AND project_id = ?", (judge, project))
    db.commit()
    held = {r[0] for r in db.execute(
        "SELECT a.project_id FROM assignments a WHERE a.event_id = ? AND a.judge_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM reviews v WHERE v.judge_id = a.judge_id AND v.project_id = a.project_id)", (FIX, judge))}
    reviewed_before = db.execute("SELECT COUNT(*) FROM reviews WHERE judge_id = ?", (judge,)).fetchone()[0]
    count = {p: db.execute("SELECT COUNT(*) FROM assignments WHERE project_id = ?", (p,)).fetchone()[0] for p in held}
    assert "Open work by judge" in organizer.get(f"/events/{FIX}/manage/assignments").text
    r = post_form(organizer, f"/events/{FIX}/manage/assignments/release", {"judge_id": judge},
                  csrf_from=f"/events/{FIX}/manage/assignments")
    assert r.status_code == 303
    for p in held:
        assert not db.execute("SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?", (judge, p)).fetchone()
    assert db.execute("SELECT COUNT(*) FROM reviews WHERE judge_id = ?", (judge,)).fetchone()[0] == reviewed_before
    r = post_form(organizer, f"/events/{FIX}/manage/assignments/release", {"judge_id": judge},
                  csrf_from=f"/events/{FIX}/manage/assignments")
    assert r.status_code == 404
    post_form(organizer, f"/events/{FIX}/manage/assignments/auto", {}, csrf_from=f"/events/{FIX}/manage/assignments")
    target = db.execute("SELECT reviews_per_project FROM events WHERE id = ?", (FIX,)).fetchone()[0]
    live = {r[0] for r in db.execute("SELECT id FROM projects WHERE event_id = ? AND duplicate_of IS NULL "
                                     "AND disqualified_reason IS NULL AND status = 'submitted'", (FIX,))}
    assert held & live
    for p in held & live:   # covered again, by other judges
        now = db.execute("SELECT COUNT(*) FROM assignments WHERE project_id = ? AND judge_id <> ?", (p, judge)).fetchone()[0]
        assert now >= min(count[p], target), (p, now, count[p])


def test_changing_your_password_ends_every_other_session_and_token(tmp_path, settings):
    from app.main import create_app
    s = dataclasses.replace(settings, data_dir=tmp_path / "real", demo=False, admin_email="owner@example.org",
                            admin_password="the first long password")
    cli.bootstrap(s, out=io.StringIO())
    app = create_app(s)

    def login(password):
        c = client_for(app)
        r = c.post("/login", data={"csrf": csrf_of(c, "/login"), "email": "owner@example.org", "password": password,
                                   "next": "/me"}, follow_redirects=False)
        return c, r.status_code

    me, code = login("the first long password")
    other, _ = login("the first long password")
    token = me.post("/api/v1/me/tokens", json={"label": "script"}).json()["token"]
    bearer = client_for(app)
    assert bearer.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    r = post_form(me, "/me/password", {"current": "wrong", "password": "a brand new password"})
    assert r.status_code == 422 and "current password is not right" in r.text
    r = post_form(me, "/me/password", {"current": "the first long password", "password": "short"})
    assert r.status_code == 422 and "at least 15" in r.text
    r = post_form(me, "/me/password", {"current": "the first long password", "password": "a brand new password"})
    assert r.status_code == 303
    assert me.get("/me").status_code == 200                                     # this browser stays in
    assert other.get("/me", follow_redirects=False).status_code == 303          # the other session is out
    assert bearer.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert login("the first long password")[1] != 303 and login("a brand new password")[1] == 303


def test_demo_passwords_cannot_be_changed(organizer):
    assert "cannot be changed here" in organizer.get("/me").text
    r = post_form(organizer, "/me/password", {"current": "plumb-demo-2026", "password": "a brand new password"})
    assert r.status_code == 409


# --- deadline extensions ---------------------------------------------------------------------


def test_a_team_extension_reopens_only_that_teams_entry(organizer, participant, db):
    from .conftest import ts
    team, project = db.execute(
        "SELECT t.id, p.id FROM teams t JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id "
        "JOIN projects p ON p.team_id = t.id AND p.duplicate_of IS NULL "
        "WHERE t.event_id = ? AND u.email = 'priya1@example.org'", (FIX,)).fetchone()
    db.execute("DELETE FROM comparisons WHERE project_a = ? OR project_b = ?", (project, project))
    db.execute("DELETE FROM reviews WHERE project_id = ?", (project,))
    db.commit()
    edit = {"title": "Glass Signal, fixed", "summary": "One line.", "description": "", "repo_url": "", "demo_url": ""}
    assert post_form(participant, f"/projects/{project}/edit", edit).status_code == 409     # the event has closed
    # Refused: before the event's own deadline, without a reason.
    close = db.execute("SELECT submissions_close FROM events WHERE id = ?", (FIX,)).fetchone()[0]
    assert organizer.post(f"/api/v1/events/{FIX}/extensions",
                          json={"team_id": team, "until": close, "reason": "x"}).status_code == 422
    assert organizer.post(f"/api/v1/events/{FIX}/extensions",
                          json={"team_id": team, "until": ts(6), "reason": ""}).status_code == 422
    r = organizer.post(f"/api/v1/events/{FIX}/extensions", json={"team_id": team, "until": ts(6), "reason": "power cut"})
    assert r.status_code == 204
    page = participant.get(f"/events/{FIX}/team").text
    assert "Your team has an extension until" in page and "power cut" in page
    assert post_form(participant, f"/projects/{project}/edit", edit).status_code == 303
    assert db.execute("SELECT title FROM projects WHERE id = ?", (project,)).fetchone()[0] == "Glass Signal, fixed"
    # The team itself stays as it was at the close.
    assert participant.post(f"/api/v1/teams/{team}/leave").status_code == 409
    # Once a judge reviews it, the entry is final again.
    judge = db.execute("SELECT judge_id FROM assignments WHERE project_id = ? LIMIT 1", (project,)).fetchone()[0]
    db.execute("INSERT INTO reviews(id, event_id, judge_id, project_id, submitted_at, updated_at) VALUES "
               "('rev_x', ?, ?, ?, ?, ?)", (FIX, judge, project, ts(0), ts(0)))
    db.commit()
    r = post_form(participant, f"/projects/{project}/edit", edit)
    assert r.status_code == 409 and "already reviewed" in r.text
    actions = [a[0] for a in db.execute("SELECT action FROM audit_log WHERE subject = ?", (team,))]
    assert "team.extension_granted" in actions
    assert organizer.delete(f"/api/v1/events/{FIX}/extensions/{team}").status_code == 204


def test_extensions_are_signed_into_the_results(organizer, db):
    from .conftest import ts
    from .test_round2 import set_event
    team = db.execute("SELECT id FROM teams WHERE event_id = ? ORDER BY id LIMIT 1", (FIX,)).fetchone()[0]
    assert organizer.post(f"/api/v1/events/{FIX}/extensions",
                          json={"team_id": team, "until": ts(6), "reason": "venue wifi failed"}).status_code == 204
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    assert organizer.post(f"/api/v1/events/{FIX}/publish").status_code == 200
    import json
    body = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'", (FIX,)).fetchone()[0])
    assert body["extensions"] == [{"team": team, "until": body["extensions"][0]["until"], "reason": "venue wifi failed"}]
    assert organizer.delete(f"/api/v1/events/{FIX}/extensions/{team}").status_code == 409   # final after publication


def test_extra_reviews_go_to_the_projects_a_close_call_hangs_on(organizer, db):
    from app import assign, domain

    from .test_round2 import _with_prizes
    _with_prizes(db)
    db.commit()
    calls = domain.close_call_candidates(db, FIX)
    targets = sorted({p for c in calls for p in c["projects"]})
    assert calls and len(targets) >= 2
    count = lambda p: db.execute("SELECT COUNT(*) FROM assignments WHERE project_id = ?", (p,)).fetchone()[0]  # noqa: E731
    before = {p: count(p) for p in targets}
    total_before = db.execute("SELECT COUNT(*) FROM assignments WHERE event_id = ?", (FIX,)).fetchone()[0]
    assert "Reviews where they decide a prize" in organizer.get(f"/events/{FIX}/manage/assignments").text
    r = post_form(organizer, f"/events/{FIX}/manage/assignments/prize-line", {}, csrf_from=f"/events/{FIX}/manage/assignments")
    assert r.status_code == 303
    import json
    short = json.loads(db.execute("SELECT detail FROM audit_log WHERE action = 'assignments.prize_line'").fetchone()[0])["short"]
    gained = {p: count(p) - before[p] for p in targets}
    # One more each, unless no qualified judge without a conflict is left in its track, which is reported.
    assert all(gained[p] == 1 or (gained[p] == 0 and "no qualified" in short[p]) for p in targets), (gained, short)
    assert sum(gained.values()) >= 1
    total_after = db.execute("SELECT COUNT(*) FROM assignments WHERE event_id = ?", (FIX,)).fetchone()[0]
    assert total_after - total_before == sum(gained.values())    # and nowhere else
    from app.domain.judging import _assignment_inputs
    js, ps, _ = _assignment_inputs(db, FIX)
    judges, projects = {j.id: j for j in js}, {p.id: p for p in ps}
    newest = db.execute("SELECT judge_id, project_id FROM assignments WHERE event_id = ? ORDER BY created_at DESC, "
                        "rowid DESC LIMIT ?", (FIX, sum(gained.values()))).fetchall()
    for j, p in newest:                                           # the same rules as automatic assignment
        assert assign.conflict(judges[j], projects[p]) is None and assign.qualified(judges[j], projects[p])


def test_a_team_sees_why_it_ranked_where_it_did(app, db):
    from .conftest import client_for
    # A team of the published replay event: log in as one of its members.
    member, project = db.execute(
        "SELECT u.id, p.id FROM projects p JOIN team_members m ON m.team_id = p.team_id JOIN users u ON u.id = m.user_id "
        "WHERE p.event_id = 'evt_replay' AND p.duplicate_of IS NULL AND p.status = 'submitted' ORDER BY p.id LIMIT 1").fetchone()
    from app.security import new_token, token_hash
    token = new_token()
    db.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?, ?, 'x', "
               "'2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')", (token_hash(token), member))
    db.commit()
    team = client_for(app)
    team.cookies.set("plumb_session", token)
    page = team.get(f"/projects/{project}/explain")
    assert page.status_code == 200 and "Why" in page.text and "Judge A" in page.text and "Counts as" in page.text
    judges = [r[0] for r in db.execute("SELECT u.name FROM reviews v JOIN users u ON u.id = v.judge_id "
                                       "WHERE v.project_id = ?", (project,))]
    assert judges and not any(name in page.text for name in judges)           # anonymous
    comments = [r[0] for r in db.execute("SELECT comment FROM reviews WHERE project_id = ? AND comment <> ''", (project,))]
    assert not any(c in page.text for c in comments)                          # comments stay with the organizers
    other = client_for(app, "participant")                                    # not on that team
    assert other.get(f"/projects/{project}/explain").status_code == 403
    unpublished = db.execute("SELECT id FROM projects WHERE event_id = 'evt_01' LIMIT 1").fetchone()[0]
    assert client_for(app, "organizer").get(f"/projects/{unpublished}/explain").status_code == 403


def test_a_new_claim_link_ends_the_earlier_one(app, organizer, db):
    _, first, _ = _claim_link(organizer, db)
    _, second, _ = _claim_link(organizer, db)
    assert first != second
    assert client_for(app).get(first).status_code == 404
    assert client_for(app).get(second).status_code == 200
