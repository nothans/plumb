"""A real v1.0 database (the demo, booted by v1.0 and saved in tests/upgrade)
upgrades in place: the migrations run, the old audit rows and signed records
still verify, its published results still read from the signed record, and
the new features work on it."""

import dataclasses
import gzip
import io
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app import audit, cli
from app.db import SCHEMA_VERSION, connect
from app.main import create_app
from app.signing import verify

from .conftest import ROOT

DATA = ROOT / "tests" / "upgrade"


@pytest.fixture
def upgraded(tmp_path, settings):
    s = dataclasses.replace(settings, data_dir=tmp_path / "old")
    s.data_dir.mkdir()
    with gzip.open(DATA / "plumb-v1.0.db.gz", "rb") as src, open(s.database_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    before = connect(s.database_path)
    assert before.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "4"
    rows_before = before.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    before.close()
    cli.bootstrap(s, out=io.StringIO())
    return s, rows_before


def test_the_migrations_run_and_the_old_chain_still_verifies(upgraded):
    s, rows_before = upgraded
    conn = connect(s.database_path)
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert {"archived_at", "rubric_locked_at", "pairwise"} <= columns
    assert {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")} >= {"v", "ip_tag", "event_prev"}
    versions = [r[0] for r in conn.execute("SELECT v FROM audit_log ORDER BY seq")]
    assert versions[:rows_before] == [1] * rows_before      # the v1.0 rows keep their format
    result = audit.verify_chain(conn)
    assert result["ok"], result


def test_every_v1_0_record_still_verifies_against_its_key(upgraded):
    s, _ = upgraded
    key = json.loads((DATA / "plumb-v1.0-key.json").read_text(encoding="utf-8"))
    conn = connect(s.database_path)
    rows = conn.execute("SELECT payload, signature, key_id FROM records").fetchall()
    assert len(rows) > 50
    assert all(r["key_id"] == key["key_id"] and verify(r["payload"], r["signature"], key["public_key_b64"]) for r in rows)


def test_the_published_v1_0_results_still_render_from_their_record(upgraded):
    s, _ = upgraded
    anon = TestClient(create_app(s), base_url="http://testserver")
    page = anon.get("/events/evt_replay/results")
    assert page.status_code == 200 and "Winners" in page.text
    api = anon.get("/api/v1/events/evt_replay/results")
    assert api.status_code == 200 and len(api.json()["ranking"]) == 40
    csv = anon.get("/events/evt_replay/export/results.csv")
    assert csv.status_code == 200 and len(csv.text.strip().splitlines()) == 41


def test_new_features_work_on_an_upgraded_database(upgraded):
    s, _ = upgraded
    app = create_app(s)
    org = TestClient(app, base_url="http://testserver")
    org.cookies.set("plumb_session", cli.DEMO_SESSIONS["organizer"][0])
    assert org.post("/api/v1/events/evt_01/archive").status_code == 204
    assert org.get("/events/evt_01/manage/judges").status_code == 200   # lists unclaimed accounts: the claim_links table exists
    conn = connect(s.database_path)
    assert conn.execute("SELECT v FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()[0] == 2
    assert audit.verify_chain(conn)["ok"]


def test_a_backup_is_taken_before_migrating(upgraded):
    s, _ = upgraded
    backup = s.database_path.with_name(f"plumb.db.before-schema-{SCHEMA_VERSION}-from-4.bak")
    assert backup.exists()
    old = connect(backup)
    assert old.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "4"
    assert "archived_at" not in {r[1] for r in old.execute("PRAGMA table_info(events)")}


def test_a_database_from_a_newer_plumb_is_refused(settings):
    from app.db import NewerDatabase
    conn = connect(settings.database_path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    with pytest.raises(NewerDatabase, match="made by a newer Plumb"):
        create_app(settings)
