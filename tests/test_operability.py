"""Running a real event: demo mode can never reach real data, the demo can be
retired without losing anything, and the defaults survive a venue."""

import dataclasses
import io

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
