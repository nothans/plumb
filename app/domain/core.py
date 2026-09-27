"""Errors, the actor, input validation and users."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import audit
from ..db import new_id, now, transaction
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)


class DomainError(Exception):
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    status = 404


class Unauthorized(DomainError):
    status = 401


class Forbidden(DomainError):
    status = 403


class Conflict(DomainError):
    status = 409


class Invalid(DomainError):
    status = 422


@dataclass(frozen=True)
class Actor:
    id: str
    email: str
    name: str
    is_admin: bool
    ip: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- small validators --------------------------------------------------------

_URL = re.compile(r"^https?://[^\s<>\"]+$", re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Control characters, and the invisible bidi and zero-width marks that can
# make one team name look like another on a signed certificate.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _as_str(value, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    if _CONTROL.search(value):
        raise Invalid(f"{field} contains control characters")
    return value


def clean_text(value: str | None, field: str, *, required: bool = False, max_len: int = 200) -> str:
    value = _as_str(value, field).strip()
    if required and not value:
        raise Invalid(f"{field} is required")
    if len(value) > max_len:
        raise Invalid(f"{field} is longer than {max_len} characters")
    return value


def clean_url(value: str | None, field: str) -> str:
    value = _as_str(value, field).strip()
    if value and not _URL.match(value):
        raise Invalid(f"{field} must be an http or https URL")
    if len(value) > 500:
        raise Invalid(f"{field} is too long")
    return value


def clean_email(value: str | None) -> str:
    value = _as_str(value, "email").strip().lower()
    if not _EMAIL.match(value) or len(value) > 254:
        raise Invalid("that does not look like an email address")
    return value


def clean_ts(value: str | None, field: str, *, required: bool = False) -> str | None:
    """Accept ISO 8601 or an HTML datetime-local value (taken as UTC)."""
    value = _as_str(value, field).strip()
    if not value:
        if required:
            raise Invalid(f"{field} is required")
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid(f"{field} is not a date and time") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- users -------------------------------------------------------------------


def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def require_user(actor: Actor | None) -> Actor:
    if actor is None:
        raise Unauthorized("log in first")
    return actor


def api_tokens(conn: sqlite3.Connection, actor: Actor | None) -> list[dict]:
    actor = require_user(actor)
    return [dict(r) for r in conn.execute(
        "SELECT label, created_at, last_used_at, substr(token_hash, 1, 8) AS hint FROM api_tokens WHERE user_id = ? "
        "ORDER BY created_at DESC", (actor.id,))]


def create_api_token(conn: sqlite3.Connection, actor: Actor | None, label: str) -> str:
    """Returns the token once; only its hash is stored."""
    actor = require_user(actor)
    label = clean_text(label, "label", required=True, max_len=60)
    token = "plb_" + new_token()
    with transaction(conn):
        conn.execute("INSERT INTO api_tokens(token_hash, user_id, label, created_at) VALUES (?,?,?,?)",
                     (token_hash(token), actor.id, label, now()))
        audit.append(conn, "api_token.created", actor_id=actor.id, subject=actor.id, detail={"label": label}, ip=actor.ip)
    return token


def revoke_api_token(conn: sqlite3.Connection, actor: Actor | None, hint: str) -> None:
    actor = require_user(actor)
    hint = clean_text(hint, "hint", required=True, max_len=64)
    with transaction(conn):
        rows = conn.execute("SELECT token_hash FROM api_tokens WHERE user_id = ? AND substr(token_hash, 1, 8) = ?",
                            (actor.id, hint[:8])).fetchall()
        if len(rows) != 1:
            raise NotFound("no such token")
        conn.execute("DELETE FROM api_tokens WHERE token_hash = ?", (rows[0]["token_hash"],))
        audit.append(conn, "api_token.revoked", actor_id=actor.id, subject=actor.id, detail={"hint": hint[:8]}, ip=actor.ip)


def create_user(
    conn: sqlite3.Connection, email: str, name: str, password_hash: str | None, *,
    is_admin: bool = False, ip: str | None = None, user_id: str | None = None, actor_id: str | None = None,
    allow_claim: bool = False,
) -> str:
    """Create an account, or claim one an import created without a password.

    Claiming is only allowed through an invitation link (allow_claim=True).
    Plumb sends no mail, so it cannot prove someone owns an address; an
    organizer handing out the link is that proof. Without this rule anyone
    could sign up as an imported judge's email and inherit the judge role.
    """
    email = clean_email(email)
    name = clean_text(name, "name", required=True, max_len=100)
    with transaction(conn):
        existing = user_by_email(conn, email)
        if existing and (existing["password_hash"] or not allow_claim):
            if existing["password_hash"]:
                raise Conflict("an account with that email already exists; log in instead")
            raise Conflict("this email is already registered for an event; ask the organizer for your invitation link")
        if existing:
            # An account created by an import, claimed through an invitation.
            conn.execute(
                "UPDATE users SET name = ?, password_hash = ? WHERE id = ?", (name, password_hash, existing["id"])
            )
            audit.append(conn, "user.claimed", actor_id=existing["id"], subject=existing["id"], ip=ip)
            return existing["id"]
        uid = user_id or new_id("usr")
        conn.execute(
            "INSERT INTO users(id, email, name, password_hash, is_admin, created_at, created_ip) VALUES (?,?,?,?,?,?,?)",
            (uid, email, name, password_hash, int(is_admin), now(), ip),
        )
        audit.append(conn, "user.created", actor_id=actor_id or uid, subject=uid, detail={"admin": is_admin}, ip=ip)
        return uid
