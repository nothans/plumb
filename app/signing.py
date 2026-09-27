"""Ed25519 signing for records the portal issues.

The private key is generated on first boot and lives in the data volume.
The public key is published at /.well-known/plumb-key.json so a record can
be checked by anyone, including by tools/verify_record.py with no server.
"""

import base64
import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key_b64 = _b64(raw)
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 key")
            return cls(key)
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        path.write_bytes(pem)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return cls(key)

    def sign(self, payload: str) -> str:
        return _b64(self._key.sign(payload.encode()))

    def public_jwk(self) -> dict:
        return {"kty": "OKP", "crv": "Ed25519", "x": self.public_key_b64, "kid": self.key_id}


def verify(payload: str, signature: str, public_key_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64)).verify(_unb64(signature), payload.encode())
        return True
    except (InvalidSignature, ValueError):
        return False
