"""Regression tests for the second review round: each test is one finding."""

import io
import ipaddress
import json
import time

import pytest

from app import cli, webhooks
from app.config import Settings

from .conftest import ROOT, post_form, ts

FIX, LIVE, REPLAY = "evt_01", "evt_demo_live", "evt_replay"


def set_event(db, event_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE events SET {sets} WHERE id = ?", (*cols.values(), event_id))


# --- votes stay secret from organizers until close ------------------------------------------


def test_votes_are_sealed_in_the_audit_log_and_revealed_after_close(app, organizer, db):
    from .conftest import signup
    v = signup(app, "sealed@example.net")
    assert v.post("/api/v1/projects/prj_03/vote").status_code == 204
    entries = organizer.get(f"/api/v1/events/{FIX}/audit?action=vote").json()["entries"]
    cast = [e for e in entries if e["action"] == "vote.cast"]
    assert cast and all(e["subject"] == "sealed" and "prj_" not in json.dumps(e) for e in cast)
    assert organizer.get(f"/api/v1/events/{FIX}/votes/reveal").status_code == 403

    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    reveal = organizer.get(f"/api/v1/events/{FIX}/votes/reveal").json()["votes"]
    seals = {e["detail"]["seal"] for e in cast}
    import hashlib
    for r in reveal:
        assert hashlib.sha256(f"{r['nonce']}:{r['project']}".encode()).hexdigest() == r["seal"]
    assert {r["seal"] for r in reveal} <= seals


def test_open_vote_cannot_be_shortened_or_switched_off(organizer):
    assert organizer.patch(f"/api/v1/events/{FIX}", json={"voting_close": ts(0.01)}).status_code == 409
    assert organizer.patch(f"/api/v1/events/{FIX}", json={"voting_mode": "off"}).status_code == 409
    assert organizer.patch(f"/api/v1/events/{FIX}", json={"voting_close": ts(24 * 30)}).status_code == 200


def test_closed_vote_cannot_be_reopened_in_two_steps(organizer, db):
    set_event(db, REPLAY, results_published_at=None)  # isolate the voting rule from the publication freeze
    assert organizer.patch(f"/api/v1/events/{REPLAY}", json={"voting_mode": "off"}).status_code == 409
    assert organizer.patch(f"/api/v1/events/{REPLAY}", json={"voting_close": ts(24)}).status_code == 409


def test_publishing_waits_for_the_vote_even_when_it_has_not_started(organizer, db):
    set_event(db, FIX, voting_open=ts(24), voting_close=ts(48))
    assert organizer.post(f"/api/v1/events/{FIX}/publish").status_code == 409


# --- publication freezes what the record describes ----------------------------------------------


def test_settings_save_after_publication_keeps_awards_and_prizes(organizer, db):
    before = db.execute("SELECT prize_id, project_id FROM awards WHERE event_id = ?", (REPLAY,)).fetchall()
    assert len(before) == 4
    page = organizer.get(f"/events/{REPLAY}/manage/settings").text
    import re
    prizes = re.search(r'<textarea name="prizes"[^>]*>([^<]*)</textarea>', page).group(1)
    form = {k: db.execute(f"SELECT {k} FROM events WHERE id = ?", (REPLAY,)).fetchone()[0] or ""
            for k in ("name", "submissions_open", "submissions_close", "judging_close", "voting_mode",
                      "voting_open", "voting_close", "votes_per_voter", "reviews_per_project", "max_team_size", "pairwise")}
    r = post_form(organizer, f"/events/{REPLAY}/manage/settings", {**form, "tagline": "edited", "prizes": prizes},
                  csrf_from=f"/events/{REPLAY}/manage/settings")
    assert r.status_code == 303, r.text[:300]
    assert db.execute("SELECT prize_id, project_id FROM awards WHERE event_id = ?", (REPLAY,)).fetchall() == before
    r = organizer.patch(f"/api/v1/events/{REPLAY}", json={"prizes": [{"name": "New prize"}]})
    assert r.status_code == 409
    assert organizer.patch(f"/api/v1/events/{REPLAY}", json={"name": "Renamed"}).status_code == 409


def test_prize_ids_survive_an_edit(organizer, db):
    ids = [r["id"] for r in db.execute("SELECT id FROM prizes WHERE event_id = ? ORDER BY position", (LIVE,))]
    r = organizer.patch(f"/api/v1/events/{LIVE}", json={"prizes": [{"name": "Best in show", "description": "the one"},
                                                                    {"name": "Best first hack"}]})
    assert r.status_code == 200
    assert [r["id"] for r in db.execute("SELECT id FROM prizes WHERE event_id = ? ORDER BY position", (LIVE,))] == ids


# --- awards --------------------------------------------------------------------------------------


def _published_fixture(organizer, db, awards):
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    db.execute("INSERT INTO prizes(id, event_id, name, position) VALUES ('prz_a', ?, 'Grand', 0), ('prz_b', ?, 'Second', 1)",
               (FIX, FIX))
    return organizer.post(f"/api/v1/events/{FIX}/publish", json={"awards": awards})


def test_one_project_cannot_win_two_overall_prizes(organizer, db):
    top = organizer.get(f"/api/v1/events/{FIX}/results").json()["ranking"]
    r = _published_fixture(organizer, db, {"prz_a": top[1]["project_id"]})
    assert r.status_code == 200
    body = json.loads(db.execute("SELECT payload FROM records WHERE event_id = ? AND kind = 'results'", (FIX,)).fetchone()[0])
    won = {a["name"]: a for a in body["awards"]}
    assert won["Grand"]["project"] == top[1]["project_id"] and won["Grand"]["overrode_suggestion"]
    # Second place follows the organizer's choice: the best remaining project, not a repeat.
    assert won["Second"]["project"] == top[0]["project_id"] and not won["Second"]["overrode_suggestion"]


def test_explicit_double_award_is_refused(organizer, db):
    top = organizer.get(f"/api/v1/events/{FIX}/results").json()["ranking"]
    pid = top[0]["project_id"]
    assert _published_fixture(organizer, db, {"prz_a": pid, "prz_b": pid}).status_code == 422


# --- demo credentials never reach production ---------------------------------------------------------


def test_demo_database_refuses_to_start_in_production_mode(settings):
    prod = Settings(**{**settings.__dict__, "demo": False})
    with pytest.raises(SystemExit) as exc:
        cli.bootstrap(prod, out=io.StringIO())
    assert "demo mode" in str(exc.value)


# --- inputs ----------------------------------------------------------------------------------------------


def test_list_valued_timestamps_and_bidi_names_are_refused(organizer, app):
    from .conftest import signup
    assert organizer.patch(f"/api/v1/events/{LIVE}", json={"submissions_open": ["a", "b"]}).status_code == 422
    assert organizer.patch(f"/api/v1/events/{LIVE}", json={"prizes": [{"name": "x", "track_id": ["a"]}]}).status_code == 422
    c = signup(app, "bidi@example.net")
    assert c.post(f"/api/v1/events/{LIVE}/teams", json={"name": "Team‮evil"}).status_code == 422


def test_host_header_never_reaches_a_signed_record(app, organizer, db):
    app.state.settings.__dict__  # frozen dataclass; build a copy without a base URL
    from dataclasses import replace
    app.state.settings = replace(app.state.settings, base_url="")
    set_event(db, FIX, voting_open=ts(-48), voting_close=ts(-1))
    r = organizer.post(f"/api/v1/events/{FIX}/publish", headers={"Host": "attacker.example"})
    assert r.status_code == 409 and "PLUMB_BASE_URL" in r.json()["error"]


# --- webhooks ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("addr", ["64:ff9b::7f00:1", "::ffff:127.0.0.1", "2002:7f00:1::1", "10.1.2.3", "127.0.0.1"])
def test_disguised_internal_addresses_are_refused(addr):
    assert not webhooks._public(ipaddress.ip_address(addr))


def test_a_trickling_receiver_costs_one_deadline_not_the_whole_queue(organizer, settings, monkeypatch):
    from app.routes import api as api_routes
    monkeypatch.setattr(api_routes, "check_destination", lambda url: None)
    organizer.post(f"/api/v1/events/{FIX}/webhooks", json={"url": "https://slow.example/hook"})
    organizer.put(f"/api/v1/events/{FIX}/rubric", json={"weights": {"quality": 2}})

    def slow(url, body, headers):
        time.sleep(3)
        return 200

    d = webhooks.Deliverer(settings.database_path, send=slow, deadline=0.3)
    started = time.monotonic()
    assert d.run_once() == 0
    assert time.monotonic() - started < 1.5
    row = __import__("sqlite3").connect(settings.database_path).execute(
        "SELECT status, last_error FROM webhook_deliveries ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "failed" and "within" in row[1]
