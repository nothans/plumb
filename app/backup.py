"""Backups that are checked when they are made, restores that are checked
before they replace anything, and a doctor that says what to look at.

    python -m app.cli backup PATH     a consistent copy, verified
    python -m app.cli restore PATH    put a verified copy back (portal stopped)
    python -m app.cli doctor          what an operator should check before an event

A backup is only half of what a restore needs: records stay verifiable only
with the signing key that issued them, so keep signing-key.pem (and
retired-keys.json, if the key was ever rotated) beside every copy.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import audit
from .config import Settings, boot_problems, forwarded_problem
from .db import SCHEMA_VERSION, check_rules, connect
from .signing import Signer, verify


class RestoreRefused(RuntimeError):
    pass


def _check(path: Path) -> dict:
    """What a database file holds, and whether it is whole: SQLite's own
    integrity check and the audit chain, recomputed from the first entry."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return {"ok": False, "problem": f"SQLite integrity check failed: {integrity}"}
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row[0]) if row else 1
        chain = audit.verify_chain(conn)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("events", "users", "reviews", "records", "audit_log")}
        keys = sorted({r[0] for r in conn.execute("SELECT DISTINCT key_id FROM records")})
        return {"ok": chain["ok"], "problem": None if chain["ok"] else f"audit chain broken at {chain.get('broken_at')}",
                "schema": version, "counts": counts, "record_keys": keys}
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "problem": f"not a Plumb database: {exc}"}
    finally:
        conn.close()


def backup(settings: Settings, target: Path) -> dict:
    target = Path(target).resolve()
    if target == settings.database_path.resolve():
        raise ValueError("that is the live database; back up to another path")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = connect(settings.database_path)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    result = _check(target)
    if not result["ok"]:
        raise RuntimeError(f"the copy at {target} did not verify: {result['problem']}")
    result["path"] = str(target)
    inside = settings.data_dir.resolve() in target.parents
    result["warning"] = ("this copy is inside the data volume, so losing the volume loses it too: copy it, with "
                         "signing-key.pem, somewhere else") if inside else None
    return result


def restore(settings: Settings, source: Path) -> dict:
    """Replace the live database with a verified backup. Stop the portal
    first: a restore under a running server would be overwritten by its next
    write. The database being replaced is kept beside it."""
    source = Path(source).resolve()
    if not source.exists():
        raise RestoreRefused(f"{source} does not exist")
    live = settings.database_path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    staged = live.with_name(f"{live.name}.restoring-{stamp}")
    copy = sqlite3.connect(staged)
    src = sqlite3.connect(source)
    unreadable = None
    try:
        with copy:
            src.backup(copy)
    except sqlite3.DatabaseError as exc:
        unreadable = exc
    finally:
        src.close()
        copy.close()
    if unreadable is not None:
        staged.unlink(missing_ok=True)   # closed first: Windows will not delete an open file
        raise RestoreRefused(f"{source} was not restored, and nothing changed: not a Plumb database ({unreadable})")
    result = _check(staged)
    problem = result["problem"] if not result["ok"] else None
    if not problem and result["schema"] > SCHEMA_VERSION:
        problem = f"the backup is at schema {result['schema']}, made by a newer Plumb (this one understands {SCHEMA_VERSION})"
    if not problem:
        conn = sqlite3.connect(staged)
        try:
            check_rules(conn, missing_ok=True)
        except RuntimeError as exc:
            problem = str(exc)
        finally:
            conn.close()
    if problem:
        staged.unlink(missing_ok=True)
        raise RestoreRefused(f"{source} was not restored, and nothing changed: {problem}")
    signer = Signer.load_or_create(settings.signing_key_path)
    unknown = [k for k in result["record_keys"] if signer.public_key_for(k) is None]
    kept = None
    if live.exists():
        kept = live.with_name(f"{live.name}.before-restore-{stamp}.bak")
        old = connect(live)
        out = sqlite3.connect(kept)
        try:
            with out:
                old.backup(out)       # includes whatever is still in the write-ahead log
        finally:
            out.close()
            old.close()
    os.replace(staged, live)
    for suffix in ("-wal", "-shm"):
        Path(f"{live}{suffix}").unlink(missing_ok=True)
    result.update(restored=str(source), kept=str(kept) if kept else None, warning=(
        f"records in this backup were signed by key(s) {', '.join(unknown)}, which this portal does not hold: "
        "restore the signing-key.pem (and retired-keys.json) that went with the backup, or those records will "
        "not verify") if unknown else None)
    return result


def doctor(settings: Settings) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) for what an operator should look at before an event."""
    out: list[tuple[str, bool, str]] = []
    problems = boot_problems(settings)
    if fwd := forwarded_problem(os.environ.get("FORWARDED_ALLOW_IPS"), settings.demo):
        problems.append(fwd)
    out.append(("settings", not problems, "; ".join(problems) or "consistent"))
    out.append(("mode", not settings.demo, "demo: the published logins are public" if settings.demo else "real event"))
    out.append(("base url", bool(settings.base_url), settings.base_url or "PLUMB_BASE_URL is not set; publishing is refused"))
    if not settings.database_path.exists():
        out.append(("database", False, f"{settings.database_path} does not exist yet; run bootstrap"))
        return out
    state = _check(settings.database_path)
    out.append(("database", state["ok"], state["problem"] or
                f"schema {state['schema']}, {state['counts']['events']} events, {state['counts']['reviews']} reviews, "
                f"{state['counts']['audit_log']} audit entries, chain verified"))
    conn = connect(settings.database_path)
    try:
        try:
            check_rules(conn)
            out.append(("rules", True, "every rule trigger is as this version defines it"))
        except RuntimeError as exc:
            out.append(("rules", False, str(exc)))
        signer = Signer.load_or_create(settings.signing_key_path)
        rows = conn.execute("SELECT payload, signature, key_id FROM records").fetchall()
        bad = 0
        for r in rows:
            found = signer.public_key_for(r["key_id"])
            bad += not (found and verify(r["payload"], r["signature"], found[0]))
        out.append(("records", bad == 0, f"{len(rows) - bad} of {len(rows)} signed records verify with this portal's keys"))
    finally:
        conn.close()
    mode = settings.signing_key_path.stat().st_mode & 0o777
    out.append(("signing key", os.name != "posix" or mode == 0o600,
                f"{settings.signing_key_path}, mode {oct(mode)}" + ("" if os.name == "posix" else " (not checked off POSIX)")))
    backups = sorted([*settings.data_dir.glob("*.bak"), *settings.data_dir.glob("backups/*.db")],
                     key=lambda p: p.stat().st_mtime)
    if backups:
        newest = datetime.fromtimestamp(backups[-1].stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")
        detail = f"{len(backups)} in the data volume, newest {newest}; keep copies off the host, with the key"
    else:
        detail = "none in the data volume; keep copies off the host, with the key"
    out.append(("backups", True, detail))
    return out
