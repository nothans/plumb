"""Judges' drafts live on the server and count for nothing; every submitted
review and entry gets a numbered receipt, signed, that anyone can check."""

import json
import sqlite3

import pytest

from .conftest import client_for, csrf_of, post_form

FIX, LIVE = "evt_01", "evt_demo_live"


@pytest.fixture
def open_review(judge_a, db):
    """A project assigned to judge_a with its review taken back, so it can be drafted and submitted."""
    me = judge_a.get("/api/judge/scores").json()["judge"]
    project = db.execute("SELECT project_id FROM reviews WHERE judge_id = ? AND event_id = ? ORDER BY project_id LIMIT 1",
                         (me, FIX)).fetchone()[0]
    db.execute("DELETE FROM reviews WHERE judge_id = ? AND project_id = ?", (me, project))
    db.commit()
    return me, project


def test_a_draft_survives_and_counts_for_nothing(judge_a, db, open_review):
    from app import domain
    me, project = open_review
    before = domain.compute_results(db, FIX)["fit"].n_observations
    r = post_form(judge_a, f"/projects/{project}/review",
                  {"score_functionality": "4", "comment": "half way", "action": "draft"}, csrf_from=f"/events/{FIX}/judge")
    assert r.status_code == 303 and r.headers["location"] == f"/projects/{project}/review"
    page = judge_a.get(f"/projects/{project}/review").text      # another visit, the same draft
    radio = page.split('id="score_functionality-4"')[1].split(">")[0]
    assert "Draft saved" in page and "half way" in page and "checked" in radio
    assert "Draft" in judge_a.get(f"/events/{FIX}/judge").text
    assert domain.compute_results(db, FIX)["fit"].n_observations == before
    # An out-of-range draft is refused like a review.
    r = post_form(judge_a, f"/projects/{project}/review", {"score_quality": "9", "action": "draft"},
                  csrf_from=f"/events/{FIX}/judge")
    assert r.status_code == 422


def test_submitting_gives_a_numbered_receipt_anyone_can_check(judge_a, judge_b, anon, db, open_review):
    me, project = open_review
    form = {"score_functionality": "4", "score_quality": "3", "score_innovation": "5", "comment": "Clear.",
            "action": "submit"}
    r = post_form(judge_a, f"/projects/{project}/review", form, csrf_from=f"/events/{FIX}/judge")
    assert r.status_code == 303 and r.headers["location"] == f"/projects/{project}/review/receipt"
    assert not db.execute("SELECT 1 FROM review_drafts WHERE judge_id = ? AND project_id = ?", (me, project)).fetchone()
    page = judge_a.get(f"/projects/{project}/review/receipt").text
    number = int(page.split("Receipt No. ")[1].split("<")[0])
    env = judge_a.get(f"/receipts/{number}.json").json()
    check = anon.post("/api/v1/verify", json=env).json()
    assert check["ok"], check
    body = json.loads(env["payload"])
    assert body["type"] == "plumb.review-receipt/v1" and body["number"] == number
    page = anon.post("/verify", data={"csrf": csrf_of(anon, "/verify"),
                                      "envelope": json.dumps(env)}).text
    assert f"receipt No. {number}, for a review of" in page
    assert body["values"] == {"functionality": 4, "quality": 3, "innovation": 5} and "Clear." not in env["payload"]
    # Opening it again is the same receipt; a change is a new one, numbered next.
    assert f"Receipt No. {number}<" in judge_a.get(f"/projects/{project}/review/receipt").text
    post_form(judge_a, f"/projects/{project}/review", {**form, "score_quality": "4"}, csrf_from=f"/events/{FIX}/judge")
    assert f"Receipt No. {number + 1}<" in judge_a.get(f"/projects/{project}/review/receipt").text
    # A draft over a submitted review is refused; another judge cannot read the receipt.
    r = post_form(judge_a, f"/projects/{project}/review", {**form, "action": "draft"}, csrf_from=f"/events/{FIX}/judge")
    assert r.status_code == 409
    assert judge_b.get(f"/receipts/{number}.json").status_code == 404
    assert anon.get(f"/receipts/{number}.json", follow_redirects=False).status_code in (303, 401)


def test_a_submitted_entry_gets_a_receipt(app, db):
    from .conftest import signup
    team = signup(app, "receipt-team@example.net", "Receipt Team", "a long team password")
    r = post_form(team, f"/events/{LIVE}/teams", {"name": "Receipt Testers"}, csrf_from=f"/events/{LIVE}/team")
    assert r.status_code == 303
    r = post_form(team, f"/events/{LIVE}/projects/new", {"title": "Receipt Machine", "summary": "Prints receipts.",
                                                          "description": "", "repo_url": "", "demo_url": "",
                                                          "action": "submit"}, csrf_from=f"/events/{LIVE}/team")
    assert r.status_code == 303 and r.headers["location"].endswith("/receipt")
    page = team.get(r.headers["location"]).text
    number = int(page.split("Receipt No. ")[1].split("<")[0])
    env = team.get(f"/receipts/{number}.json").json()
    body = json.loads(env["payload"])
    assert body["type"] == "plumb.entry-receipt/v1" and body["project"]["title"] == "Receipt Machine"
    assert client_for(app).post("/api/v1/verify", json=env).json()["ok"]


def test_receipts_are_numbered_in_order_and_never_change(judge_a, db, open_review):
    me, project = open_review
    post_form(judge_a, f"/projects/{project}/review", {"score_functionality": "4", "score_quality": "3",
                                                       "score_innovation": "5", "action": "submit"},
              csrf_from=f"/events/{FIX}/judge")
    judge_a.get(f"/projects/{project}/review/receipt")
    with pytest.raises(sqlite3.IntegrityError, match="receipts cannot change"):
        db.execute("UPDATE receipts SET payload = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="receipts cannot be deleted"):
        db.execute("DELETE FROM receipts")
    with pytest.raises(sqlite3.IntegrityError, match="numbered in order"):
        db.execute("INSERT INTO receipts(number, event_id, kind, subject, content_sha256, payload, signature, key_id, "
                   "issued_at) VALUES (99, ?, 'review', 'x', 'h', '{}', 's', 'k', 't')", (FIX,))
