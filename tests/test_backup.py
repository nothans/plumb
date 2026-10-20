"""Backup and restore, tested the way an operator would use them: back up
while the portal runs, lose something, restore, and check that what came
back is whole (audit chain, signatures) and that nothing else was lost."""

import gzip
import shutil
import sqlite3

import pytest

from app import audit, backup, cli
from app.db import SCHEMA_VERSION, connect
from app.signing import Signer, verify

from .conftest import ROOT


@pytest.fixture(autouse=True)
def cli_settings(settings, monkeypatch):
    """The CLI reads its settings from the environment; point it at the test portal's."""
    monkeypatch.setattr(cli, "load_settings", lambda: settings)


def test_backup_then_restore_brings_back_exactly_the_copy(app, settings, organizer, tmp_path, capsys):
    copy = tmp_path / "offsite" / "plumb-backup.db"
    assert cli.main(["backup", str(copy)]) == 0
    assert "verified" in capsys.readouterr().out
    # Work goes on after the backup, then disaster.
    assert organizer.post("/api/v1/events/evt_01/archive").status_code == 204
    conn = connect(settings.database_path)
    after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    assert cli.main(["restore", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "restored" in out and "kept at" in out
    conn = connect(settings.database_path)
    assert conn.execute("SELECT archived_at FROM events WHERE id = 'evt_01'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] < after
    assert audit.verify_chain(conn)["ok"]
    signer = Signer.load_or_create(settings.signing_key_path)
    rows = conn.execute("SELECT payload, signature FROM records").fetchall()
    assert rows and all(verify(r["payload"], r["signature"], signer.public_key_b64) for r in rows)
    # The database that was replaced is kept, with the archive in it.
    kept = next(settings.data_dir.glob("plumb.db.before-restore-*.bak"))
    old = sqlite3.connect(kept)
    assert old.execute("SELECT archived_at FROM events WHERE id = 'evt_01'").fetchone()[0] is not None


def test_a_backup_inside_the_data_volume_is_flagged(settings):
    result = backup.backup(settings, settings.data_dir / "copy.db")
    assert result["ok"] and "inside the data volume" in result["warning"]


@pytest.mark.parametrize("damage", ["garbage", "broken chain", "newer schema"])
def test_a_bad_backup_is_refused_and_nothing_changes(settings, tmp_path, damage):
    bad = tmp_path / "bad.db"
    if damage == "garbage":
        bad.write_bytes(b"this is not a database" * 100)
    else:
        backup.backup(settings, bad)
        conn = sqlite3.connect(bad)
        if damage == "broken chain":
            conn.execute("DROP TRIGGER audit_no_update")
            conn.execute("UPDATE audit_log SET detail = '{}' WHERE seq = 3")
        else:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
    before = settings.database_path.read_bytes()
    with pytest.raises(backup.RestoreRefused, match="nothing changed"):
        backup.restore(settings, bad)
    assert settings.database_path.read_bytes() == before
    assert not list(settings.data_dir.glob("plumb.db.restoring-*"))


def test_restoring_records_signed_by_another_key_says_to_bring_the_key(settings, tmp_path):
    # The v1.0 demo database was signed by a key this portal never held.
    old = tmp_path / "v1.0.db"
    with gzip.open(ROOT / "tests" / "upgrade" / "plumb-v1.0.db.gz", "rb") as src, open(old, "wb") as dst:
        shutil.copyfileobj(src, dst)
    result = backup.restore(settings, old)
    assert result["warning"] and "restore the signing-key.pem" in result["warning"]


def test_the_doctor_reports_what_matters(settings, capsys):
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    for check in ("settings", "mode", "database", "rules", "records", "signing key"):
        assert check in out
    assert "chain verified" in out and "signed records verify" in out
    assert code == 2 and "FIX  mode" in out      # the test portal runs in demo mode, which a real event must not
