"""The database keeps the rules on its own (app/schema.sql, rule_ triggers):
each is broken here in raw SQL, with no domain code in the way, and must be
refused. A rule changed by hand stops the app from starting."""

import sqlite3

import pytest

from app import db as dbmod
from app.main import create_app

from .conftest import ts

FIX, REPLAY = "evt_01", "evt_replay"


def refused(db, sql, *args, message="plumb:"):
    with pytest.raises(sqlite3.IntegrityError, match=message):
        db.execute(sql, args)


def test_a_score_outside_its_range_is_refused(db):
    review, crit = db.execute(
        "SELECT s.review_id, s.criterion_id FROM review_scores s JOIN reviews r ON r.id = s.review_id "
        "WHERE r.event_id = ? LIMIT 1", (FIX,)).fetchone()
    refused(db, "UPDATE review_scores SET value = 9 WHERE review_id = ? AND criterion_id = ?", review, crit,
            message="outside its criterion's range")
    refused(db, "UPDATE review_scores SET value = 0 WHERE review_id = ? AND criterion_id = ?", review, crit)


def test_published_reviews_scores_votes_and_awards_are_frozen(db):
    review = db.execute("SELECT id FROM reviews WHERE event_id = ? LIMIT 1", (REPLAY,)).fetchone()[0]
    refused(db, "UPDATE reviews SET comment = 'changed' WHERE id = ?", review, message="reviews are frozen")
    refused(db, "DELETE FROM reviews WHERE id = ?", review)
    refused(db, "UPDATE review_scores SET value = value WHERE review_id = ?", review)
    refused(db, "DELETE FROM review_scores WHERE review_id = ?", review)
    refused(db, "DELETE FROM votes WHERE event_id = ?", REPLAY, message="votes are final")
    refused(db, "UPDATE awards SET project_id = project_id WHERE event_id = ?", REPLAY, message="awards are final")
    refused(db, "DELETE FROM awards WHERE event_id = ?", REPLAY)
    refused(db, "UPDATE events SET results_published_at = NULL WHERE id = ?", REPLAY, message="cannot be undone")


def test_signed_records_never_change(db):
    refused(db, "UPDATE records SET payload = payload || ' '", message="signed records cannot change")
    refused(db, "DELETE FROM records", message="cannot be deleted")


def test_the_rubric_freezes_with_reviews_and_with_the_lock(db):
    refused(db, "INSERT INTO criteria(id, event_id, key, name) VALUES ('crt_x', ?, 'extra', 'Extra')", FIX,
            message="criteria can be reweighted but not added")
    refused(db, "DELETE FROM criteria WHERE event_id = ?", FIX, message="cannot be removed")
    db.execute("UPDATE criteria SET weight = 2 WHERE event_id = ?", (FIX,))   # reweighting is allowed until the lock
    db.execute("UPDATE events SET rubric_locked_at = ? WHERE id = ?", (ts(0), FIX))
    refused(db, "UPDATE criteria SET weight = 3 WHERE event_id = ?", FIX, message="the rubric is locked")
    refused(db, "UPDATE events SET rubric_locked_at = NULL WHERE id = ?", FIX, message="cannot be undone")


def test_votes_are_cast_inside_the_window(db):
    voter = db.execute("SELECT id FROM users WHERE email = 'priya1@example.org'").fetchone()[0]
    db.execute("UPDATE events SET voting_mode = 'accounts', voting_open = ?, voting_close = ? WHERE id = ?",
               (ts(-48), ts(-1), FIX))
    insert = "INSERT INTO votes(event_id, voter_id, project_id, created_at) VALUES (?, ?, 'prj_01', ?)"
    refused(db, insert, FIX, voter, ts(0), message="voting is not open")
    refused(db, insert, FIX, voter, ts(-49))
    db.execute(insert, (FIX, voter, ts(-24)))


def test_an_entry_is_submitted_by_the_deadline_or_its_extension(db):
    close, team = db.execute("SELECT e.submissions_close, p.team_id FROM projects p JOIN events e ON e.id = p.event_id "
                             "WHERE p.id = 'prj_02'").fetchone()
    late = close.replace(close[:4], str(int(close[:4]) + 1), 1)
    refused(db, "UPDATE projects SET submitted_at = ? WHERE id = 'prj_02'", late, message="submissions are closed")
    db.execute("INSERT INTO team_extensions(team_id, event_id, until, reason, granted_at) VALUES (?, ?, ?, 'power cut', ?)",
               (team, FIX, late, ts(0)))
    db.execute("UPDATE projects SET submitted_at = ? WHERE id = 'prj_02'", (late,))


def test_an_audit_entry_must_link_to_the_last(db):
    last = db.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    insert = ("INSERT INTO audit_log(seq, at, actor_id, action, subject, detail, prev_hash, v, hash) "
              "VALUES (?, ?, NULL, 'forged', 'x', '{}', ?, 2, 'f')")
    refused(db, insert, last["seq"] + 1, ts(0), "0" * 64, message="must link to the one before it")
    refused(db, insert, last["seq"] + 2, ts(0), last["hash"])     # a gap in the sequence


def test_every_rule_in_the_schema_is_in_the_database(db):
    held = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    rules = set(dbmod.schema_triggers())
    assert len([r for r in rules if r.startswith("rule_")]) == 27 and rules <= held


def test_a_rule_weakened_by_hand_stops_the_app(settings, db):
    db.execute("DROP TRIGGER rule_records_no_delete")
    db.execute("CREATE TRIGGER rule_records_no_delete BEFORE DELETE ON records BEGIN SELECT 1; END")
    with pytest.raises(dbmod.RulesChanged, match="rule_records_no_delete"):
        create_app(settings)


def test_a_dropped_rule_comes_back_at_boot(settings, db):
    db.execute("DROP TRIGGER rule_records_no_delete")
    create_app(settings)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'rule_records_no_delete'").fetchone()


# --- the registered judging method -------------------------------------------------------------


def test_locking_the_rubric_registers_the_method_and_publishes_its_hash(organizer, anon, db):
    import hashlib
    import json

    from app import method

    from .test_round2 import set_event
    assert anon.get(f"/events/{FIX}/method.json").status_code == 404
    assert organizer.post(f"/api/v1/events/{FIX}/rubric/lock").status_code == 204
    digest = db.execute("SELECT method_hash FROM events WHERE id = ?", (FIX,)).fetchone()[0]
    body = anon.get(f"/events/{FIX}/method.json")
    assert hashlib.sha256(body.content).hexdigest() == digest == body.headers["x-plumb-method-sha256"]
    assert json.loads(body.content)["probabilities_withheld_below_dof"] == 20
    assert digest in anon.get(f"/events/{FIX}").text
    refused(db, "UPDATE events SET method_hash = 'x' WHERE id = ?", FIX, message="cannot change")
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    assert organizer.post(f"/api/v1/events/{FIX}/publish").status_code == 200
    m = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'",
                              (FIX,)).fetchone()[0])["method"]
    assert m["registered"]["sha256"] == digest and m["matches_registered"] is True
    assert method.digest(m["description"]) == m["sha256"] == digest
    assert "These results were produced by exactly that method." in anon.get(f"/events/{FIX}/results").text


def test_a_method_changed_after_registration_shows_in_the_record(organizer, db, monkeypatch):
    import json

    from app import method

    from .test_round2 import set_event
    assert organizer.post(f"/api/v1/events/{FIX}/rubric/lock").status_code == 204
    monkeypatch.setattr(method, "CLOSE_CALL", 0.5)   # say, an upgrade moved the close-call line
    original = method.describe
    monkeypatch.setattr(method, "describe", lambda c: {**original(c), "close_call_below": method.CLOSE_CALL})
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    assert organizer.post(f"/api/v1/events/{FIX}/publish").status_code == 200
    m = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'",
                              (FIX,)).fetchone()[0])["method"]
    assert m["matches_registered"] is False and m["sha256"] != m["registered"]["sha256"]
