"""The extended checker verifies signatures with its own pure-Python Ed25519;
it must agree with the real library, both ways."""

import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

import acceptance_extended as ext  # noqa: E402


def test_pure_python_ed25519_agrees_with_the_library():
    for _ in range(5):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        msg = os.urandom(64)
        sig = key.sign(msg)
        assert ext.ed25519_verify(pub, msg, sig)
        assert not ext.ed25519_verify(pub, msg + b"x", sig)
        assert not ext.ed25519_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
        other = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert not ext.ed25519_verify(other, msg, sig)
