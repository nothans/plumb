"""Export, then import into the same portal as a copy: nothing may be lost."""

from .conftest import client_for, csrf_of


def admin_client(app):
    c = client_for(app)
    c.post("/login", data={"csrf": csrf_of(c, "/login"), "email": "admin@example.org",
                           "password": "plumb-demo-2026", "next": "/"})
    return c


def test_round_trip_keeps_duplicate_decisions_drafts_and_every_field(app, organizer, db):
    # An organizer decision the auto-detection would not reproduce: keep the later entry.
    assert organizer.post("/api/v1/events/evt_01/duplicates", json={"keep": "prj_41", "drop": "prj_07"}).status_code == 200
    # A reviewed entry that is no longer submitted (possible via an import of such data).
    db.execute("UPDATE projects SET status = 'draft', submitted_at = NULL, description = 'long text', "
               "demo_url = 'https://demo.example/x' WHERE id = 'prj_05'")
    db.execute("UPDATE projects SET disqualified_reason = 'plagiarism' WHERE id = 'prj_09'")

    doc = organizer.get("/events/evt_01/export/event.json").json()
    doc["event"]["id"] = "evt_copy"
    admin = admin_client(app)
    r = admin.post("/api/v1/events/import", json=doc)
    assert r.status_code == 201, r.text

    copy = admin.get("/events/evt_copy/export/event.json").json()
    by_title = lambda d: {(p["title"], p["submitted_at"]): p for p in d["projects"]}
    orig, new = by_title(doc), by_title(copy)
    assert orig.keys() == new.keys()
    for key, p in orig.items():
        q = new[key]
        for field in ("summary", "description", "repo_url", "demo_url", "status", "disqualified_reason", "track"):
            if field == "track":
                continue  # track ids are remapped in the copy
            assert p[field] == q[field], (key, field)
    # The kept/dropped choice survived: the later Dry Harbour is still the live one.
    new_ids = {p["id"]: (p["title"], p["submitted_at"]) for p in copy["projects"]}
    dup = copy["plumb"]["duplicates"]
    assert len(dup) == 1
    assert new_ids[dup[0]["duplicate_of"]] == ("Dry Harbour", "2026-03-01T17:57:00Z")
    # The draft kept its reviews.
    draft_new_id = next(p["id"] for p in copy["projects"] if p["title"] == orig[next(k for k in orig if orig[k]["id"] == "prj_05")]["title"])
    assert any(s["project"] == draft_new_id for s in copy["scores"])
