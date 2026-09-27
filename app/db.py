"""SQLite access: one connection per request, explicit transactions."""

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 4

# Numbered migrations for databases created by an older version. schema.sql
# always describes the current shape (and is idempotent), so a new database
# needs none of these; an old one runs the ones above its stored version.
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE events ADD COLUMN pairwise INTEGER NOT NULL DEFAULT 0 CHECK (pairwise IN (0, 1))"],
    3: ["ALTER TABLE votes ADD COLUMN nonce TEXT"],
    4: ["ALTER TABLE events ADD COLUMN rubric_locked_at TEXT"],
}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def init_schema(conn: sqlite3.Connection) -> None:
    has_events = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events'").fetchone()
    if has_events:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row[0]) if row else 1
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
