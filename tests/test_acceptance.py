"""The seven DOGFOOD checks, against the routes in .dogfood.toml, plus the
reasons behind each answer (a check can pass for the wrong reason)."""

import tomllib
from pathlib import Path

from .conftest import ROOT

CFG = tomllib.loads((ROOT / ".dogfood.toml").read_text(encoding="utf-8"))
R = CFG["routes"]


def test_gallery_is_public_and_shows_fixture_titles(anon):
    r = anon.get(R["gallery"])
    assert r.status_code == 200
    for title in ("Glass Signal", "Salt Loom"):
        assert title in r.text


def test_closed_event_refuses_submission_because_it_is_closed(participant):
    r = participant.post(R["submit"], json={"title": "late", "summary": "probe"})
    assert r.status_code == 409
    assert "closed" in r.json()["error"]


def test_judge_reads_own_scores(judge_a):
    r = judge_a.get(R["judge_scores"])
    assert r.status_code == 200
    assert sum(1 for x in r.json()["reviews"] if x["event_id"] == "evt_01") == 11


def test_judge_cannot_read_peer_scores(judge_b):
    r = judge_b.get(R["peer_scores"])
    assert r.status_code == 403


def test_participant_blocked_from_judge_route(participant, anon):
    assert participant.get(R["judge_scores"]).status_code == 403
    assert anon.get(R["judge_scores"]).status_code == 401


def test_organizer_exports_csv(organizer, participant):
    r = organizer.get(R["csv_export"])
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert lines[0].startswith("event_id,project_id")
    assert len(lines) == 1 + 126
    assert participant.get(R["csv_export"]).status_code == 403


def test_organizer_can_read_a_judges_scores(organizer):
    assert organizer.get(R["peer_scores"]).status_code == 200
