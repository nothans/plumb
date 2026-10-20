"""Running a real event: demo mode can never reach real data, the demo can be
retired without losing anything, and the defaults survive a venue."""

import dataclasses
import io
import json
import os
import re

import pytest

from app import cli, ratelimit, webhooks
from app.db import connect

from .conftest import client_for, csrf_of


def _boot(settings, **changes):
    s = dataclasses.replace(settings, **changes)
    out = io.StringIO()
    cli.bootstrap(s, out=out)
    return out.getvalue()


@pytest.fixture
def blank(tmp_path, settings):
    """Settings for an empty, never-seeded volume."""
    return dataclasses.replace(settings, data_dir=tmp_path / "prod", demo=False)


def test_demo_mode_refuses_to_seed_a_database_that_holds_real_data(blank):
    _boot(blank, admin_email="owner@example.org", admin_password="a real password here")
    conn = connect(blank.database_path)
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # The same volume, started by mistake with the demo compose file.
    with pytest.raises(SystemExit) as refused:
        _boot(blank, demo=True)
    assert "refusing to seed public demo logins" in str(refused.value)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before
    assert conn.execute("SELECT 1 FROM users WHERE email = 'admin@example.org'").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM meta WHERE key = 'demo_seeded'").fetchone() is None


def test_demo_mode_ignores_the_production_admin_settings(blank):
    out = _boot(blank, demo=True, admin_email="owner@example.org", admin_password="a real password here")
    assert "ignoring PLUMB_ADMIN_EMAIL" in out
    conn = connect(blank.database_path)
    assert conn.execute("SELECT 1 FROM users WHERE email = 'owner@example.org'").fetchone() is None


def test_a_demo_database_with_real_work_is_retired_not_deleted(settings, app):
    conn = connect(settings.database_path)
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert client_for(app, "organizer").get("/api/v1/me").status_code == 200

    with pytest.raises(SystemExit) as refused:
        _boot(settings, demo=False)
    message = str(refused.value)
    assert "retire-demo" in message and "Nothing was changed" in message

    result = cli.retire_demo(conn)
    assert result["sessions_ended"] == len(cli.DEMO_SESSIONS)
    assert result["admins_demoted"] == 1 and result["accounts_locked"] > 0
    # Every public way in is gone; every piece of data is still there.
    for role in cli.DEMO_SESSIONS:
        assert client_for(app, role).get("/api/v1/me").status_code == 401
    login = client_for(app)
    r = login.post("/login", data={"csrf": csrf_of(login, "/login"), "email": "admin@example.org",
                                   "password": cli.DEMO_PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 401
    assert not conn.execute("SELECT is_admin FROM users WHERE email = 'admin@example.org'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == reviews
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'demo.retired'").fetchone()

    # Now it boots privately, and demo mode can never re-seed it.
    _boot(settings, demo=False)
    with pytest.raises(SystemExit):
        _boot(settings, demo=True)


def test_logging_out_of_a_shared_demo_session_does_not_end_it_for_everyone(app):
    org = client_for(app, "organizer")
    r = org.post("/logout", data={"csrf": csrf_of(org, "/me")}, follow_redirects=False)
    assert r.status_code == 303
    # The next person holding the published cookie (the checker, another judge) is still in.
    r = client_for(app, "organizer").get("/events/evt_01/export/scores.csv", follow_redirects=False)
    assert r.status_code == 200 and r.text.startswith("event_id,")


def test_a_normal_session_still_ends_on_logout(app):
    from .conftest import signup
    alice = signup(app, "logout@example.net")
    token = alice.cookies.get("plumb_session")
    alice.post("/logout", data={"csrf": csrf_of(alice, "/me")}, follow_redirects=False)
    again = client_for(app)
    again.cookies.set("plumb_session", token)
    assert again.get("/api/v1/me").status_code == 401


def test_webhook_host_exemption_only_applies_in_demo_mode(monkeypatch):
    monkeypatch.setenv("PLUMB_WEBHOOKS_ALLOW_HOSTS", "host.docker.internal")
    monkeypatch.setenv("PLUMB_DEMO", "0")
    assert webhooks._allowed_hosts() == set()
    monkeypatch.setenv("PLUMB_DEMO", "1")
    assert webhooks._allowed_hosts() == {"host.docker.internal"}


def test_rate_limits_survive_a_venue_and_can_be_configured(app):
    limiter = ratelimit.RateLimiter()
    # A whole event behind one address signs up without waiting an hour.
    for _ in range(200):
        ratelimit.check(limiter, "signup", "203.0.113.1")
    # The per-email guessing defence is unchanged.
    for _ in range(10):
        ratelimit.check(limiter, "login", "someone@example.org")
    with pytest.raises(ratelimit.TooMany):
        ratelimit.check(limiter, "login", "someone@example.org")

    limits = ratelimit.parse_limits("signup=2/60, vote=5/10")
    assert limits["signup"] == (2, 60.0) and limits["vote"] == (5, 10.0)
    assert limits["login"] == ratelimit.DEFAULT_LIMITS["login"]
    small = ratelimit.RateLimiter(limits)
    ratelimit.check(small, "signup", "x")
    ratelimit.check(small, "signup", "x")
    with pytest.raises(ratelimit.TooMany):
        ratelimit.check(small, "signup", "x")
    for bad in ("signup", "signup=x/1", "nosuch=1/1", "signup=0/10"):
        with pytest.raises(ValueError):
            ratelimit.parse_limits(bad)


# --- archive ------------------------------------------------------------------------------


def test_an_archived_event_leaves_the_listings_but_keeps_its_links(app, organizer, judge_a, anon, db):
    fixture_title = db.execute("SELECT title FROM projects WHERE id = 'prj_01'").fetchone()[0]
    assert 'href="/projects/prj_01"' in anon.get("/projects?q=" + fixture_title).text
    assert judge_a.post("/api/v1/events/evt_01/archive").status_code == 403
    assert organizer.post("/api/v1/events/evt_01/archive").status_code == 204
    assert organizer.post("/api/v1/events/evt_01/archive").status_code == 409
    assert "evt_01" not in [e["id"] for e in anon.get("/api/v1/events").json()]
    assert "evt_01" in [e["id"] for e in anon.get("/api/v1/events?archived=true").json()]
    assert 'href="/events/evt_01"' not in anon.get("/").text and "1 archived event" in anon.get("/").text
    assert 'href="/events/evt_01"' in anon.get("/?archived=1").text
    page = anon.get("/events/evt_01")
    assert page.status_code == 200 and "This event was archived" in page.text
    assert "evt_01" in judge_a.get("/api/v1/me").json()["events"]    # an archive never hides your own roles
    assert 'href="/projects/prj_01"' not in anon.get("/projects?q=" + fixture_title).text
    assert 'href="/projects/prj_01"' in anon.get("/projects?event=evt_01").text
    assert organizer.delete("/api/v1/events/evt_01/archive").status_code == 204
    assert "evt_01" in [e["id"] for e in anon.get("/api/v1/events").json()]
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE event_id = 'evt_01' AND action LIKE 'event.%'")]
    assert actions[-2:] == ["event.archived", "event.restored"]


def test_the_gallery_leads_with_the_newest_event(anon, db):
    newest = db.execute("SELECT e.id FROM events e JOIN projects p ON p.event_id = e.id "
                        "WHERE e.archived_at IS NULL ORDER BY e.created_at DESC, e.id DESC LIMIT 1").fetchone()[0]
    first = re.search(r'href="/projects/(prj_[^"]+)"', anon.get("/projects").text).group(1)
    assert db.execute("SELECT event_id FROM projects WHERE id = ?", (first,)).fetchone()[0] == newest


# --- signing keys ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_the_key_file_is_private_from_the_first_byte(tmp_path):
    from app.signing import Signer
    Signer.load_or_create(tmp_path / "k" / "signing-key.pem")
    assert (tmp_path / "k" / "signing-key.pem").stat().st_mode & 0o777 == 0o600


def test_a_rotated_key_keeps_old_records_verifiable(settings, anon):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.signing import Signer
    conn = connect(settings.database_path)
    old = conn.execute("SELECT id, key_id FROM records LIMIT 1").fetchone()
    env = anon.get(f"/records/{old['id']}.json").json()
    before = Signer.load_or_create(settings.signing_key_path)
    new = Signer.rotate(settings.signing_key_path)
    assert new.key_id != before.key_id == old["key_id"]
    assert b"PRIVATE" in settings.signing_key_path.read_bytes()     # the new key; the old private half is gone
    client = TestClient(create_app(settings), base_url="http://testserver")
    keys = client.get("/.well-known/plumb-key.json").json()["keys"]
    assert [k["kid"] for k in keys] == [new.key_id, before.key_id] and "retired_at" in keys[1]
    result = client.post("/api/v1/verify", json=env).json()
    assert result["ok"] and "retired" in result["reason"]
    assert client.get(f"/records/{old['id']}.json").json()["public_key"] == before.public_key_b64
    # A key the portal never used, or a payload re-serialized, is refused.
    assert not client.post("/api/v1/verify", json={**env, "key_id": "0" * 16}).json()["ok"]
    loose = json.dumps(json.loads(env["payload"]), indent=1)
    assert not client.post("/api/v1/verify", json={**env, "payload": loose}).json()["ok"]


# --- housekeeping -------------------------------------------------------------------------


def test_idle_rate_limit_keys_are_dropped(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    limiter = ratelimit.RateLimiter()
    for i in range(500):
        limiter.hit("login", f"user{i}@example.org", 10, 300)
    assert len(limiter._hits) == 500
    clock[0] += 3601 + 60       # past the longest window
    limiter.hit("login", "fresh@example.org", 10, 300)
    assert set(limiter._hits) == {"login:fresh@example.org"}


def test_expired_sessions_are_deleted_when_a_session_starts(app, db):
    db.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) "
               "SELECT 'old-token', id, 'x', '2020-01-01T00:00:00Z', '2020-01-15T00:00:00Z' FROM users LIMIT 1")
    db.commit()
    c = client_for(app)
    c.post("/login", data={"csrf": csrf_of(c, "/login"), "email": "admin@example.org", "password": "plumb-demo-2026",
                           "next": "/"})
    assert not db.execute("SELECT 1 FROM sessions WHERE token_hash = 'old-token'").fetchone()
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] > 0   # the live ones stay


def test_the_dashboard_refreshes_only_when_asked(organizer):
    assert organizer.get("/api/v1/events/evt_01").json()["phase"]["judging_open"]
    page = organizer.get("/events/evt_01/manage").text
    assert 'http-equiv="refresh"' not in page and "Refresh every minute" in page
    page = organizer.get("/events/evt_01/manage?refresh=1").text
    assert 'http-equiv="refresh"' in page and "Stop refreshing" in page


@pytest.mark.parametrize("value, ok", [
    ("127.0.0.1", True), ("172.16.0.0/12,127.0.0.1", True), ("::1", True), ("", True),
    ("*", False), ("172.16.0.5/12", False), ("10.0.0.0/33", False), ("proxy", False),
])
def test_forwarded_allow_ips_is_checked_like_uvicorn_reads_it(value, ok):
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    from app.config import forwarded_problem
    assert (forwarded_problem(value, demo=False) is None) == ok
    if not ok and value != "*":
        # What the check refuses is exactly what uvicorn would silently never match.
        assert value in _TrustedHosts(value).trusted_literals


def test_a_wildcard_proxy_setting_stops_a_real_boot(blank, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(SystemExit, match="walk around the rate limits"):
        _boot(blank)


@pytest.mark.parametrize("base_url, secure, refused", [
    ("https://judging.example.org", False, "PLUMB_SECURE_COOKIES is off"),
    ("http://judging.example.org", True, "nobody could stay logged in"),
    ("judging.example.org", False, "must start with http"),
    ("https://judging.example.org", True, None),
    ("http://10.0.0.5:8080", False, None),     # a venue LAN without TLS is allowed
])
def test_a_real_instance_refuses_contradictory_settings(blank, base_url, secure, refused):
    if refused is None:
        _boot(blank, base_url=base_url, secure_cookies=secure)
        return
    with pytest.raises(SystemExit, match=refused):
        _boot(blank, base_url=base_url, secure_cookies=secure)
    assert not blank.database_path.exists()     # refused before touching anything
