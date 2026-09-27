"""Webhook delivery: the audit log, streamed.

A background thread walks each webhook's cursor forward through its event's
audit entries and POSTs them one at a time, in order. A failure stops that
hook where it is and retries with exponential backoff (capped at ten
minutes), so a receiver never sees entry n+1 before entry n.

Body: the audit entry as JSON. Header X-Plumb-Signature: sha256=<hex HMAC
of the exact body bytes, keyed with the hook's secret>.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .db import connect, now

Sender = Callable[[str, bytes, dict], int]
MAX_BACKOFF = 600.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is a failed delivery, not a new destination


_opener = urllib.request.build_opener(_NoRedirect)


def http_send(url: str, body: bytes, headers: dict) -> int:
    check_destination(url)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def check_destination(url: str, allow_private: bool | None = None) -> None:
    """Refuse loopback, link-local, private and reserved addresses, so a
    webhook cannot be aimed at the server itself or the network behind it.
    PLUMB_WEBHOOKS_ALLOW_PRIVATE=1 lifts this for a receiver on your LAN."""
    if allow_private is None:
        allow_private = os.environ.get("PLUMB_WEBHOOKS_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")
    if allow_private:
        return
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError("webhook url has no host")
    for info in socket.getaddrinfo(host, None):
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise ValueError(f"webhook host {host} resolves to a non-public address ({addr})")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Deliverer:
    def __init__(self, db_path: Path, send: Sender = http_send):
        self.db_path = db_path
        self.send = send
        self.retry_at: dict[str, float] = {}
        self.failures: dict[str, int] = {}

    def run_once(self, batch: int = 50, budget: float = 5.0) -> int:
        """Deliver what is due. Returns how many entries were delivered."""
        conn = connect(self.db_path)
        delivered = 0
        try:
            hooks = conn.execute("SELECT * FROM webhooks").fetchall()
            for hook in hooks:
                if time.monotonic() < self.retry_at.get(hook["id"], 0):
                    continue
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE event_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (hook["event_id"], hook["cursor"], batch),
                ).fetchall()
                started = time.monotonic()
                for row in rows:
                    if time.monotonic() - started > budget:
                        break  # one slow receiver must not hold up every other hook
                    body = json.dumps({
                        "seq": row["seq"], "at": row["at"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                        "action": row["action"], "subject": row["subject"], "detail": json.loads(row["detail"]),
                        "hash": row["hash"], "prev_hash": row["prev_hash"],
                    }, separators=(",", ":")).encode()
                    headers = {"Content-Type": "application/json", "User-Agent": "Plumb-Webhook/1",
                               "X-Plumb-Event": row["action"], "X-Plumb-Signature": sign(hook["secret"], body)}
                    try:
                        status = self.send(hook["url"], body, headers)
                        ok = 200 <= status < 300
                        error = None if ok else f"HTTP {status}"
                    except Exception as exc:  # network errors of every kind
                        ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
                    attempts = self.failures.get(hook["id"], 0) + 1
                    conn.execute(
                        "INSERT INTO webhook_deliveries(webhook_id, audit_seq, status, attempts, last_error, updated_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(webhook_id, audit_seq) DO UPDATE SET "
                        "status = excluded.status, attempts = excluded.attempts, last_error = excluded.last_error, "
                        "updated_at = excluded.updated_at",
                        (hook["id"], row["seq"], "delivered" if ok else "failed", attempts, error, now()),
                    )
                    if not ok:
                        self.failures[hook["id"]] = attempts
                        self.retry_at[hook["id"]] = time.monotonic() + min(MAX_BACKOFF, 2.0 ** attempts)
                        break
                    self.failures.pop(hook["id"], None)
                    self.retry_at.pop(hook["id"], None)
                    conn.execute("UPDATE webhooks SET cursor = ? WHERE id = ?", (row["seq"], hook["id"]))
                    delivered += 1
        finally:
            conn.close()
        return delivered


def start(db_path: Path, interval: float = 2.0) -> threading.Event:
    """Run the deliverer on a daemon thread. Set the returned event to stop it."""
    stop = threading.Event()
    deliverer = Deliverer(db_path)

    def loop() -> None:
        while not stop.is_set():
            try:
                deliverer.run_once()
            except Exception:  # never let the thread die; the next pass retries
                logging.getLogger("plumb.webhooks").exception("webhook delivery pass failed")
            stop.wait(interval)

    threading.Thread(target=loop, name="plumb-webhooks", daemon=True).start()
    return stop
