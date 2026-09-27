"""The audit trail: append-only, hash-chained, readable.

Every state change an organizer might later need to explain (a deadline
moved, a judge reassigned, a score edited, a vote cast, a comment hidden)
appends one row here inside the same transaction as the change itself.
"""

import hashlib
import sqlite3

from . import canonical
from .db import now

GENESIS = "0" * 64


def row_hash(row: dict) -> str:
    body = {k: row.get(k) for k in ("seq", "at", "event_id", "actor_id", "action", "subject", "detail", "ip", "prev_hash")}
    return hashlib.sha256(canonical.dumps(body).encode()).hexdigest()


def append(
    conn: sqlite3.Connection,
    action: str,
    *,
    actor_id: str | None,
    event_id: str | None = None,
    subject: str = "",
    detail: dict | None = None,
    ip: str | None = None,
) -> dict:
    """Append one entry. Call inside the transaction that made the change."""
    assert conn.in_transaction, "audit.append must run inside a transaction"
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    row = {
        "seq": (last["seq"] + 1) if last else 1,
        "at": now(),
        "event_id": event_id,
        "actor_id": actor_id,
        "action": action,
        "subject": subject,
        "detail": canonical.dumps(detail or {}),
        "ip": ip,
        "prev_hash": last["hash"] if last else GENESIS,
    }
    row["hash"] = row_hash(row)
    conn.execute(
        "INSERT INTO audit_log(seq, at, event_id, actor_id, action, subject, detail, ip, prev_hash, hash) "
        "VALUES (:seq, :at, :event_id, :actor_id, :action, :subject, :detail, :ip, :prev_hash, :hash)",
        row,
    )
    return row


def head(conn: sqlite3.Connection) -> dict:
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    return {"seq": last["seq"], "hash": last["hash"]} if last else {"seq": 0, "hash": GENESIS}


def verify_chain(conn: sqlite3.Connection) -> dict:
    """Recompute every hash. Returns {"ok": bool, "checked": n, "broken_at": seq|None}."""
    prev = GENESIS
    expected_seq = 1
    checked = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        row = dict(r)
        if row["seq"] != expected_seq or row["prev_hash"] != prev or row_hash(row) != row["hash"]:
            return {"ok": False, "checked": checked, "broken_at": row["seq"]}
        prev = row["hash"]
        expected_seq += 1
        checked += 1
    return {"ok": True, "checked": checked, "broken_at": None}
