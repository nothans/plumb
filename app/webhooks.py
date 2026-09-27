"""Webhook delivery: the audit log, streamed.

A background thread walks each webhook's cursor forward through its event's
audit entries and POSTs them one at a time, in order. A failure stops that
hook where it is and retries with exponential backoff (capped at ten
minutes), so a receiver never sees entry n+1 before entry n.

Body: the audit entry as JSON. Header X-Plumb-Signature: sha256=<hex HMAC
of the exact body bytes, keyed with the hook's secret>.

Safety:
* The destination is resolved once, every address it resolves to must be
  public, and the connection goes to that checked address (TLS is still
  verified against the hostname). A DNS answer that changes between the
  check and the connect cannot redirect a delivery inside the network.
* Redirects are not followed.
* Every delivery has a hard wall-clock deadline. A receiver that trickles
  its response forever costs one deadline, then backs off; it cannot hold
  up the other hooks.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from .db import connect, now

Sender = Callable[[str, bytes, dict], int]
MAX_BACKOFF = 600.0
DEADLINE = 10.0

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _allow_private() -> bool:
    return os.environ.get("PLUMB_WEBHOOKS_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def _public(addr: ipaddress._BaseAddress) -> bool:
    if isinstance(addr, ipaddress.IPv6Address):
        if any(addr in net for net in _NAT64) or addr.teredo:
            return False
        embedded = addr.ipv4_mapped or addr.sixtofour
        if embedded is not None:
            return embedded.is_global
    return addr.is_global


def resolve_checked(host: str, port: int, allow_private: bool | None = None) -> str:
    """Resolve host once and return an address to connect to, refusing any
    host that resolves to a loopback, private, link-local or reserved
    address (unless PLUMB_WEBHOOKS_ALLOW_PRIVATE=1)."""
    if allow_private is None:
        allow_private = _allow_private()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError(f"webhook host {host} does not resolve")
    addrs = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not allow_private:
        for addr in addrs:
            if not _public(addr):
                raise ValueError(f"webhook host {host} resolves to a non-public address ({addr})")
    return str(addrs[0])


def check_destination(url: str, allow_private: bool | None = None) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("webhook url must be http or https with a host")
    resolve_checked(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), allow_private)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed address, with the certificate checked for the hostname."""

    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def http_send(url: str, body: bytes, headers: dict) -> int:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("webhook url must be http or https with a host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    address = resolve_checked(parts.hostname, port)
    if parts.scheme == "https":
        conn = _PinnedHTTPS(parts.hostname, address, port, timeout=5)
    else:
        conn = http.client.HTTPConnection(address, port, timeout=5)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("POST", path, body=body, headers={**headers, "Host": parts.netloc})
        return conn.getresponse().status  # a 3xx is a failure: redirects are not followed
    finally:
        conn.close()


def _with_deadline(fn: Callable[[], int], deadline: float) -> tuple[int | None, str | None, threading.Thread | None]:
    """Run fn on a daemon thread; give up waiting after deadline seconds.
    Returns (status, error, still_running_thread)."""
    box: dict = {}

    def run() -> None:
        try:
            box["status"] = fn()
        except Exception as exc:  # network errors of every kind
            box["error"] = f"{type(exc).__name__}: {exc}"[:300]

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        return None, f"no complete response within {deadline:.0f} seconds", t
    return box.get("status"), box.get("error"), None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Deliverer:
    def __init__(self, db_path: Path, send: Sender = http_send, deadline: float = DEADLINE):
        self.db_path = db_path
        self.send = send
        self.deadline = deadline
        self.retry_at: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.stuck: dict[str, threading.Thread] = {}  # a timed-out delivery still hanging on

    def run_once(self, batch: int = 50) -> int:
        """Deliver what is due. Returns how many entries were delivered."""
        conn = connect(self.db_path)
        delivered = 0
        try:
            for hook in conn.execute("SELECT * FROM webhooks").fetchall():
                hid = hook["id"]
                if time.monotonic() < self.retry_at.get(hid, 0):
                    continue
                if hid in self.stuck:
                    if self.stuck[hid].is_alive():
                        continue  # never two requests in flight to one receiver
                    del self.stuck[hid]
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE event_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (hook["event_id"], hook["cursor"], batch),
                ).fetchall()
                for row in rows:
                    body = json.dumps({
                        "seq": row["seq"], "at": row["at"], "event_id": row["event_id"], "actor_id": row["actor_id"],
                        "action": row["action"], "subject": row["subject"], "detail": json.loads(row["detail"]),
                        "hash": row["hash"], "prev_hash": row["prev_hash"],
                    }, separators=(",", ":")).encode()
                    headers = {"Content-Type": "application/json", "User-Agent": "Plumb-Webhook/1",
                               "X-Plumb-Event": row["action"], "X-Plumb-Signature": sign(hook["secret"], body)}
                    status, error, hanging = _with_deadline(
                        lambda url=hook["url"], b=body, h=headers: self.send(url, b, h), self.deadline)
                    if hanging is not None:
                        self.stuck[hid] = hanging
                    ok = error is None and status is not None and 200 <= status < 300
                    if not ok and error is None:
                        error = f"HTTP {status}"
                    attempts = self.failures.get(hid, 0) + 1
                    conn.execute(
                        "INSERT INTO webhook_deliveries(webhook_id, audit_seq, status, attempts, last_error, updated_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(webhook_id, audit_seq) DO UPDATE SET "
                        "status = excluded.status, attempts = excluded.attempts, last_error = excluded.last_error, "
                        "updated_at = excluded.updated_at",
                        (hid, row["seq"], "delivered" if ok else "failed", attempts, error, now()),
                    )
                    if not ok:
                        self.failures[hid] = attempts
                        self.retry_at[hid] = time.monotonic() + min(MAX_BACKOFF, 2.0 ** attempts)
                        break
                    self.failures.pop(hid, None)
                    self.retry_at.pop(hid, None)
                    conn.execute("UPDATE webhooks SET cursor = ? WHERE id = ?", (row["seq"], hid))
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
