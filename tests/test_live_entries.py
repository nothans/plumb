"""One definition of a live entry, and settings forms that save what they show."""

import json
import re

from app import cli, domain, records, transfer
from app.signing import Signer

from .conftest import client_for, post_form

FIX = "evt_01"


def _form_fields(html: str) -> dict:
    """What a browser would send back from the settings form, untouched."""
    form = html[html.index('action="/events/evt_01/manage/settings"'):]
    form = form[:form.index("</form>")]
    data = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', form):
        tag, name = m.group(0), m.group(1)
        if 'type="checkbox"' in tag and "checked" not in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        data[name] = value.group(1) if value else ""
    for m in re.finditer(r'<textarea name="([^"]+)"[^>]*>([^<]*)</textarea>', form):
        data[m.group(1)] = m.group(2)
    selected = re.search(r'<select name="voting_mode">.*?<option value="([^"]+)" selected', form, re.S)
    data["voting_mode"] = selected.group(1)
    return data


def test_saving_the_settings_form_untouched_changes_nothing(app, db):
    org = client_for(app, "organizer")
    before = dict(db.execute("SELECT * FROM events WHERE id = ?", (FIX,)).fetchone())
    assert before["voting_close"][17:19] != "00" or before["voting_open"][17:19] != "00", "the demo vote carries seconds"
    seq = db.execute("SELECT MAX(seq) FROM audit_log").fetchone()[0]

    data = _form_fields(org.get(f"/events/{FIX}/manage/settings").text)
    # The vote is open: a save that read the times at minute precision would be
    # refused as shortening it.
    r = org.post(f"/events/{FIX}/manage/settings", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    after = dict(db.execute("SELECT * FROM events WHERE id = ?", (FIX,)).fetchone())
    assert after == before
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE seq > ? AND action = 'event.updated'",
                      (seq,)).fetchone()[0] == 0


def test_a_minute_precision_client_does_not_move_deadlines(app, db):
    org = client_for(app, "organizer")
    data = _form_fields(org.get(f"/events/{FIX}/manage/settings").text)
    for key in ("submissions_open", "submissions_close", "judging_close", "voting_open", "voting_close"):
        if data.get(key):
            data[key] = data[key][:16]          # what an older browser sends
    data["tagline"] = "A new tagline"
    before = dict(db.execute("SELECT * FROM events WHERE id = ?", (FIX,)).fetchone())
    r = org.post(f"/events/{FIX}/manage/settings", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    after = dict(db.execute("SELECT * FROM events WHERE id = ?", (FIX,)).fetchone())
    assert {k for k in before if before[k] != after[k]} == {"tagline"}
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'event.updated' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    assert '"tagline"' in detail and "voting_close" not in detail and '"prizes":null' in detail


def test_a_disqualified_entry_leaves_every_public_list(app, db):
    anon = client_for(app)
    total = anon.get(f"/api/v1/projects?event={FIX}").json()["total"]
    pid, title = db.execute("SELECT id, title FROM projects WHERE event_id = ? AND duplicate_of IS NULL "
                            "AND status = 'submitted' ORDER BY id LIMIT 1", (FIX,)).fetchone()
    assert title in anon.get(f"/projects?event={FIX}").text

    org = client_for(app, "organizer")
    r = post_form(org, f"/projects/{pid}/disqualify", {"reason": "plagiarised"}, csrf_from=f"/events/{FIX}/manage/integrity")
    assert r.status_code == 303

    assert anon.get(f"/api/v1/projects?event={FIX}").json()["total"] == total - 1
    assert pid not in {p["id"] for p in anon.get(f"/api/v1/projects?event={FIX}&limit=200").json()["projects"]}
    assert f"/projects/{pid}" not in anon.get(f"/projects?event={FIX}").text
    assert f"/projects/{pid}" not in anon.get(f"/embed/events/{FIX}?limit=48").text
    assert f"Gallery ({total - 1})" in anon.get(f"/events/{FIX}").text
    # The organizer still sees it, with the reason, where it can be reinstated.
    assert title in org.get(f"/events/{FIX}/manage/integrity").text


def test_a_disqualified_team_gets_no_certificate(app, db):
    org = client_for(app, "organizer")
    event = "evt_replay2"
    # The fixture event once more, published with one entry disqualified.
    fixtures = json.loads(app.state.settings.fixtures_path.read_text(encoding="utf-8"))
    admin = domain.user_by_email(db, cli.DEMO_ADMIN)
    transfer.import_event(db, fixtures, actor_id=admin["id"], event_id=event,
                          organizer_ids=[domain.user_by_email(db, "organizer@example.org")["id"]])
    actor = domain.Actor(id=domain.user_by_email(db, "organizer@example.org")["id"], email="organizer@example.org",
                         name="Olu", is_admin=False)
    pid, team = db.execute("SELECT id, team_id FROM projects WHERE event_id = ? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
                           (event,)).fetchone()
    domain.set_disqualified(db, actor, pid, "broke the rules")
    records.publish_results(db, actor, event, Signer.load_or_create(app.state.settings.signing_key_path),
                            "http://testserver")
    teams = {r["subject"] for r in records.records_for(db, event, "team")}
    assert team not in teams and len(teams) > 30
    assert org.get(f"/events/{event}/records").status_code == 200
