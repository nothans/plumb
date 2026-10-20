"""The declared access policy (app/access.py), proved against the running app:
every route is called as an anonymous visitor and as a participant who is
neither a judge, an organizer nor an admin, and must refuse whatever its
level does not allow. A route nobody declared stops the app from starting."""

import re

import pytest
from fastapi import APIRouter

from app import access
from app.main import create_app

FIX = "evt_01"


@pytest.fixture
def ids(db):
    """Real ids from the fixture event, so a refusal comes from the policy,
    not from a missing row."""
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "event_id": FIX,
        "project_id": one("SELECT id FROM projects WHERE event_id = ? AND duplicate_of IS NULL ORDER BY id LIMIT 1", FIX),
        "team_id": one("SELECT id FROM teams WHERE event_id = ? ORDER BY id LIMIT 1", FIX),
        "judge_id": one("SELECT user_id FROM event_roles WHERE event_id = ? AND role = 'judge' LIMIT 1", FIX),
        "comment_id": one("SELECT c.id FROM comments c JOIN projects p ON p.id = c.project_id LIMIT 1"),
        "record_id": one("SELECT id FROM records LIMIT 1"),
        "hook_id": "whk_000000000000", "number": "1", "token": "not-a-real-token", "code": "not-a-real-code", "hint": "00000000",
    }


def _calls(ids):
    for (method, path), level in sorted(access.ACCESS.items()):
        url = re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], path)
        yield method, path, url, level


def _call(client, method, url):
    return client.request(method, url, json={} if method in ("POST", "PUT", "PATCH") else None,
                          follow_redirects=False)


def _refused_anonymously(r) -> bool:
    if r.status_code == 401:
        return True
    return r.status_code == 303 and r.headers["location"].startswith("/login")


def test_every_route_is_declared_and_nothing_stale(app):
    served = set(access.routes_of(app))
    assert served == set(access.ACCESS)


def test_anonymous_visitors_get_only_what_is_public(anon, ids):
    wrong = []
    for method, path, url, level in _calls(ids):
        r = _call(anon, method, url)
        if level in (access.PUBLIC, access.LINK):
            # A login with no credentials is refused for being wrong, which is not the policy refusing.
            if _refused_anonymously(r) and (method, path) != ("POST", "/login"):
                wrong.append((method, path, level, r.status_code))
        elif not _refused_anonymously(r):
            wrong.append((method, path, level, r.status_code))
    assert not wrong, wrong


def test_a_participant_cannot_reach_judge_organizer_or_admin_routes(participant, ids):
    wrong = []
    for method, path, url, level in _calls(ids):
        if level not in (access.JUDGE, access.ORGANIZER, access.ADMIN):
            continue
        r = _call(participant, method, url)
        if r.status_code != 403:
            wrong.append((method, path, level, r.status_code))
    assert not wrong, wrong


def test_the_policy_refuses_before_the_body_is_read(participant):
    # A body that would fail validation still gets the policy's answer, not a 422.
    r = participant.post(f"/api/v1/events/{FIX}/publish", json={"awards": "not an object"})
    assert r.status_code == 403


def test_an_undeclared_route_stops_the_app_from_starting(settings, monkeypatch):
    from app.routes import public
    planted = APIRouter()
    planted.routes.extend(public.router.routes)

    @planted.get("/events/{event_id}/secret-report")
    def secret_report(event_id: str):
        return {"oops": event_id}

    monkeypatch.setattr(public, "router", planted)
    with pytest.raises(RuntimeError, match="undeclared GET /events/{event_id}/secret-report"):
        create_app(settings)


def test_a_declaration_for_a_route_that_is_gone_stops_it_too(settings, monkeypatch):
    monkeypatch.setitem(access.ACCESS, ("GET", "/events/{event_id}/renamed-away"), access.PUBLIC)
    with pytest.raises(RuntimeError, match="declared but not served GET /events/{event_id}/renamed-away"):
        create_app(settings)
