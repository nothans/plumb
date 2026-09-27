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
