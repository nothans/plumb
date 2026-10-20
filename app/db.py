"""SQLite access: one connection per request, explicit transactions."""

import re
import secrets
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 7

# Numbered migrations for databases created by an older version. schema.sql
# always describes the current shape (and is idempotent), so a new database
# needs none of these; an old one runs the ones above its stored version.
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE events ADD COLUMN pairwise INTEGER NOT NULL DEFAULT 0 CHECK (pairwise IN (0, 1))"],
    3: ["ALTER TABLE votes ADD COLUMN nonce TEXT"],
    4: ["ALTER TABLE events ADD COLUMN rubric_locked_at TEXT"],
    5: [
        # API tokens expire; tokens made before expiry existed get 90 days from the upgrade.
        "ALTER TABLE api_tokens ADD COLUMN expires_at TEXT",
        "UPDATE api_tokens SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+90 days') WHERE expires_at IS NULL",
        # Audit rows from now on hash v2 (see audit.py); older rows keep v1 and still verify.
        "ALTER TABLE audit_log ADD COLUMN v INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE audit_log ADD COLUMN ip_tag TEXT",
        "ALTER TABLE audit_log ADD COLUMN event_prev TEXT",
    ],
    6: ["ALTER TABLE events ADD COLUMN archived_at TEXT"],
    7: ["ALTER TABLE events ADD COLUMN method_json TEXT", "ALTER TABLE events ADD COLUMN method_hash TEXT"],
}


# The portal's one clock. Everything that asks "what time is it" (phases,
# deadlines, timestamps, the audit log) asks here, so the demo's replay event
# can be run through the real domain functions at the times it describes.
_clock: ContextVar[datetime | None] = ContextVar("plumb_clock", default=None)


def utcnow() -> datetime:
    return _clock.get() or datetime.now(UTC)


@contextmanager
def clock_at(when: datetime):
    """Run the enclosed calls as if it were `when` (seeding and tests only)."""
    token = _clock.set(when.astimezone(UTC))
    try:
        yield
    finally:
        _clock.reset(token)


def now() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def to_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class NewerDatabase(RuntimeError):
    pass


class RulesChanged(RuntimeError):
    pass


def _norm(sql: str) -> str:
    sql = re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()
    return sql.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)


def schema_triggers() -> dict[str, str]:
    """The triggers schema.sql defines, by name, normalized."""
    return {m.group(1): _norm(m.group(0)) for m in re.finditer(
        r"CREATE TRIGGER IF NOT EXISTS (\w+) .*?\bEND;", SCHEMA.read_text(encoding="utf-8"), re.S)}


def check_rules(conn: sqlite3.Connection, *, missing_ok: bool = False) -> None:
    """Refuse to run if a rule trigger in the database is not the one
    schema.sql defines. A missing one is recreated by the schema on every
    boot; one that was dropped and recreated weaker under the same name
    would not be, so it is compared here, text against text. missing_ok is
    for a database not yet booted by this version (a backup being
    restored), whose next boot adds what it lacks."""
    held = {name: _norm(sql) for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    changed = sorted(name for name, sql in schema_triggers().items()
                     if held.get(name) != sql and not (missing_ok and name not in held))
    if changed:
        raise RulesChanged("the database's rule triggers differ from this version's: " + ", ".join(changed)
                           + ". Someone changed them by hand; restore a backup, or recreate them from app/schema.sql "
                           "after finding out why.")


def _backup_before_migrating(conn: sqlite3.Connection, current: int) -> Path | None:
    """Copy the database aside before the first migration touches it, so an
    upgrade that goes wrong can be undone by putting the copy back."""
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if not path.name:
        return None   # in memory
    target = path.with_name(f"{path.name}.before-schema-{SCHEMA_VERSION}-from-{current}.bak")
    if target.exists():
        return target
    copy = sqlite3.connect(target)
    with copy:
        conn.backup(copy)
    copy.close()
    return target


def init_schema(conn: sqlite3.Connection) -> None:
    has_events = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events'").fetchone()
    if has_events:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row[0]) if row else 1
        if current > SCHEMA_VERSION:
            # Older code would write rows the newer schema's rules (and the
            # audit chain's format) do not expect. Refuse instead of guessing.
            raise NewerDatabase(f"this database is at schema {current}, made by a newer Plumb; this version "
                                f"understands up to {SCHEMA_VERSION}. Run the newer version, or restore a backup.")
        if current < SCHEMA_VERSION:
            _backup_before_migrating(conn, current)
        for version in sorted(v for v in MIGRATIONS if v > current):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    check_rules(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE so that read-then-write checks cannot race."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
