"""Password hashing and opaque tokens. Standard library only."""

import hashlib
import hmac
import secrets

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
# NIST SP 800-63B-4: a password that is the only factor, as in Plumb, is at
# least 15 characters; at least 64 must be accepted. The cap only stops a
# megabyte "password" from tying up scrypt.
MIN_PASSWORD = 15
MAX_PASSWORD = 256


def password_problem(password: str, email: str = "") -> str | None:
    """Why a new password is refused, or None. Every way a password is set
    goes through this: signup, invitation, claim link, create-admin,
    set-password, and PLUMB_ADMIN_PASSWORD. Besides length it refuses what
    an attacker tries first against this site: its own name, the account's
    address, and one character repeated. No composition rules (NIST again)."""
    if len(password) < MIN_PASSWORD:
        return f"use a password of at least {MIN_PASSWORD} characters (a few words together work well)"
    if len(password) > MAX_PASSWORD:
        return f"use a password of at most {MAX_PASSWORD} characters"
    low = password.lower()
    local = email.split("@")[0].lower()
    if "plumb" in low or (len(local) >= 4 and local in low) or len(set(password)) == 1:
        return "that password is too easy to guess here: leave out the site's name and your address"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same time as a real check so absent accounts are not
        # distinguishable by timing.
        hashlib.scrypt(password.encode(), salt=b"\0" * 16, **_SCRYPT)
        return False
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
