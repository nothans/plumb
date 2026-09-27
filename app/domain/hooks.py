"""Webhook registration."""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import new_id, now, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
    Invalid,
    NotFound,
    clean_url,
)
from .events import (  # noqa: F401
    require_organizer,
)


def webhooks_for(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> list[dict]:
    require_organizer(conn, event_id, actor)
    out = []
    for h in conn.execute("SELECT * FROM webhooks WHERE event_id = ? ORDER BY created_at", (event_id,)):
        last = conn.execute("SELECT * FROM webhook_deliveries WHERE webhook_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                            (h["id"],)).fetchone()
        out.append({**{k: h[k] for k in ("id", "url", "cursor", "created_at")}, "last": dict(last) if last else None})
    return out


def create_webhook(conn: sqlite3.Connection, actor: Actor | None, event_id: str, url: str, check) -> dict:
    """check(url) raises ValueError/OSError for a destination that is not allowed."""
    actor = require_organizer(conn, event_id, actor)
    url = clean_url(url, "url")
    if not url:
        raise Invalid("url is required")
    try:
        check(url)
    except (ValueError, OSError) as exc:
        raise Invalid(f"webhook url refused: {exc}") from exc
    import secrets as _secrets
    hid, secret = new_id("whk"), _secrets.token_hex(24)
    with transaction(conn):
        conn.execute(
            "INSERT INTO webhooks(id, event_id, url, secret, cursor, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (hid, event_id, url, secret, audit.head(conn)["seq"], actor.id, now()),
        )
        audit.append(conn, "webhook.created", actor_id=actor.id, event_id=event_id, subject=hid, detail={"url": url}, ip=actor.ip)
    return {"id": hid, "url": url, "secret": secret}


def delete_webhook(conn: sqlite3.Connection, actor: Actor | None, event_id: str, hook_id: str) -> None:
    actor = require_organizer(conn, event_id, actor)
    with transaction(conn):
        cur = conn.execute("DELETE FROM webhooks WHERE id = ? AND event_id = ?", (hook_id, event_id))
        if cur.rowcount == 0:
            raise NotFound("no such webhook")
        audit.append(conn, "webhook.deleted", actor_id=actor.id, event_id=event_id, subject=hook_id, ip=actor.ip)
