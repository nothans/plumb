"""Ed25519 signing for records the portal issues.

The private key is generated on first boot and lives in the data volume,
readable by its owner only. The public key is published at
/.well-known/plumb-key.json so a record can be checked by anyone, including
by tools/verify_record.py with no server.

Keys can be rotated (python -m app.cli rotate-key). The retired key's public
half is kept in retired-keys.json beside the key and stays published, so
every record it signed still verifies; its private half is deleted, so it
can sign nothing more.
"""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _retired_path(path: Path) -> Path:
    return path.with_name("retired-keys.json")


def _write_private(path: Path, key: Ed25519PrivateKey) -> None:
    """Create the key file readable by its owner only, from the first byte
    (never written world-readable and narrowed afterwards)."""
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)


class Signer:
    def __init__(self, key: Ed25519PrivateKey, retired: list[dict] | None = None):
        self._key = key
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key_b64 = _b64(raw)
        self.key_id = _key_id(raw)
        self.retired = retired or []   # [{"kid", "x", "retired_at"}], oldest first

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        retired_file = _retired_path(path)
        retired = json.loads(retired_file.read_text(encoding="utf-8")) if retired_file.exists() else []
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 key")
            return cls(key, retired)
        key = Ed25519PrivateKey.generate()
        _write_private(path, key)
        return cls(key, retired)

    @classmethod
    def rotate(cls, path: Path) -> "Signer":
        """Retire the current key and start a new one. The old public key is
        kept (and published) so its records still verify; the old private
        key is deleted."""
        old = cls.load_or_create(path)
        retired = [*old.retired, {"kid": old.key_id, "x": old.public_key_b64,
                                  "retired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        tmp = _retired_path(path).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(retired, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, _retired_path(path))
        path.unlink()
        return cls.load_or_create(path)

    def sign(self, payload: str) -> str:
        return _b64(self._key.sign(payload.encode()))

    def public_jwk(self) -> dict:
        return {"kty": "OKP", "crv": "Ed25519", "x": self.public_key_b64, "kid": self.key_id}

    def jwks(self) -> list[dict]:
        """The current key, then every retired one with when it was retired."""
        return [self.public_jwk(), *({"kty": "OKP", "crv": "Ed25519", "x": r["x"], "kid": r["kid"],
                                      "retired_at": r["retired_at"]} for r in reversed(self.retired))]

    def public_key_for(self, key_id: str) -> tuple[str, str | None] | None:
        """(public key, retired_at or None) for a key this portal has used, or None."""
        if key_id == self.key_id:
            return self.public_key_b64, None
        return next(((r["x"], r["retired_at"]) for r in self.retired if r["kid"] == key_id), None)


def verify(payload: str, signature: str, public_key_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64)).verify(_unb64(signature), payload.encode())
        return True
    except (InvalidSignature, ValueError):
        return False
