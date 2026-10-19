"""The audit trail: append-only, hash-chained, readable, and checkable from
outside the portal.

Every state change an organizer might later need to explain (a deadline
moved, a judge reassigned, a score edited, a vote cast, a comment hidden)
appends one row here inside the same transaction as the change itself.

Each row's hash covers the row and the hash of the row before it, so
editing or deleting any past row breaks every hash after it. Rows written
since v1.1 (column v = 2) also cover the hash of the previous row of the
same event (event_prev), and cover the client address only as a keyed tag
(ip_tag), never the address itself. That makes a single event's slice of the
log, which is what a webhook receiver gets, checkable on its own: every row's
hash recomputes from the fields delivered, and each row names the one before
it in that event, so a receiver keeping its copy is the outside witness that
an operator rewriting the database cannot also rewrite.

    v1: sha256(canonical({seq, at, event_id, actor_id, action, subject, detail, ip, prev_hash}))
    v2: sha256(canonical({seq, at, event_id, actor_id, action, subject, detail, ip_tag,
                          prev_hash, event_prev, v}))

detail is the canonical JSON string stored in the row; canonical() is
canonical.dumps (sorted keys, no spaces, UTF-8).
"""

import hashlib
import hmac
import json
import secrets
import sqlite3

from . import canonical
from .db import now

GENESIS = "0" * 64
V1_FIELDS = ("seq", "at", "event_id", "actor_id", "action", "subject", "detail", "ip", "prev_hash")
V2_FIELDS = ("seq", "at", "event_id", "actor_id", "action", "subject", "detail", "ip_tag", "prev_hash",
             "event_prev", "v")


def row_hash(row: dict) -> str:
    fields = V2_FIELDS if row.get("v") == 2 else V1_FIELDS
    body = {k: row.get(k) for k in fields}
    return hashlib.sha256(canonical.dumps(body).encode()).hexdigest()


def _ip_key(conn: sqlite3.Connection) -> bytes:
    row = conn.execute("SELECT value FROM meta WHERE key = 'audit_ip_key'").fetchone()
    if row:
        return bytes.fromhex(row["value"])
    key = secrets.token_bytes(32)
    conn.execute("INSERT INTO meta(key, value) VALUES ('audit_ip_key', ?)", (key.hex(),))
    return key


def ip_tag(conn: sqlite3.Connection, ip: str | None) -> str | None:
    """The address as the hash chain sees it: a keyed tag, the same for the
    same address, that no receiver can turn back into the address."""
    if not ip:
        return None
    return hmac.new(_ip_key(conn), ip.encode(), hashlib.sha256).hexdigest()[:32]


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
    event_prev = None
    if event_id is not None:
        prev = conn.execute("SELECT hash FROM audit_log WHERE event_id = ? ORDER BY seq DESC LIMIT 1",
                            (event_id,)).fetchone()
        event_prev = prev["hash"] if prev else GENESIS
    row = {
        "seq": (last["seq"] + 1) if last else 1,
        "at": now(),
        "event_id": event_id,
        "actor_id": actor_id,
        "action": action,
        "subject": subject,
        "detail": canonical.dumps(detail or {}),
        "ip": ip,
        "ip_tag": ip_tag(conn, ip),
        "prev_hash": last["hash"] if last else GENESIS,
        "event_prev": event_prev,
        "v": 2,
    }
    row["hash"] = row_hash(row)
    conn.execute(
        "INSERT INTO audit_log(seq, at, event_id, actor_id, action, subject, detail, ip, ip_tag, prev_hash, "
        "event_prev, v, hash) VALUES (:seq, :at, :event_id, :actor_id, :action, :subject, :detail, :ip, :ip_tag, "
        ":prev_hash, :event_prev, :v, :hash)",
        row,
    )
    return row


def head(conn: sqlite3.Connection) -> dict:
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    return {"seq": last["seq"], "hash": last["hash"]} if last else {"seq": 0, "hash": GENESIS}


def _check(rows, prev: str, expected_seq: int, last_by_event: dict, key: bytes | None) -> dict:
    checked = 0
    for r in rows:
        row = dict(r)
        broken = (row["seq"] != expected_seq or row["prev_hash"] != prev or row_hash(row) != row["hash"])
        if not broken and row.get("v") == 2:
            if row["event_id"] is not None and row["event_prev"] != last_by_event.get(row["event_id"], GENESIS):
                broken = True
            if key is not None and row["ip_tag"] != (
                    hmac.new(key, row["ip"].encode(), hashlib.sha256).hexdigest()[:32] if row["ip"] else None):
                broken = True
        if broken:
            return {"ok": False, "checked": checked, "broken_at": row["seq"]}
        if row["event_id"] is not None:
            last_by_event[row["event_id"]] = row["hash"]
        prev = row["hash"]
        expected_seq += 1
        checked += 1
    return {"ok": True, "checked": checked, "broken_at": None, "prev": prev, "next_seq": expected_seq}


def verify_chain(conn: sqlite3.Connection, *, full: bool = True) -> dict:
    """Recompute the chain. Returns {"ok", "checked", "broken_at", "full"}.

    full=True walks every row (the CLI, the published records). full=False
    starts from the checkpoint the last full walk left, after checking that
    the checkpoint row itself is unchanged, so the organizer pages stay fast
    on a log of any length; every full walk moves the checkpoint forward.
    """
    key_row = conn.execute("SELECT value FROM meta WHERE key = 'audit_ip_key'").fetchone()
    key = bytes.fromhex(key_row["value"]) if key_row else None
    start = None
    if not full:
        cp = conn.execute("SELECT value FROM meta WHERE key = 'audit_checkpoint'").fetchone()
        if cp:
            start = json.loads(cp["value"])
            row = conn.execute("SELECT hash FROM audit_log WHERE seq = ?", (start["seq"],)).fetchone()
            if row is None or row["hash"] != start["hash"]:
                return {"ok": False, "checked": 0, "broken_at": start["seq"], "full": False}
    if start:
        rows = conn.execute("SELECT * FROM audit_log WHERE seq > ? ORDER BY seq", (start["seq"],))
        result = _check(rows, start["hash"], start["seq"] + 1, dict(start["events"]), key)
        result["checked"] += start["seq"]
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq")
        last_by_event: dict = {}
        result = _check(rows, GENESIS, 1, last_by_event, key)
        if result["ok"] and result["checked"]:
            cp = {"seq": result["next_seq"] - 1, "hash": result["prev"], "events": last_by_event, "at": now()}
            conn.execute("INSERT INTO meta(key, value) VALUES ('audit_checkpoint', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cp),))
    return {"ok": result["ok"], "checked": result["checked"], "broken_at": result["broken_at"], "full": start is None}
