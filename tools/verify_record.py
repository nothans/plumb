#!/usr/bin/env python3
"""Verify a Plumb record envelope without trusting the Plumb server.

    python tools/verify_record.py envelope.json --key <base64url public key>

Get the envelope from a record's "Download envelope" link (/records/<id>.json).
Get the key once, from /.well-known/plumb-key.json, and keep it: checking a
record against a key fetched from the same server at the same moment only
proves the server agrees with itself.

Needs the `cryptography` package (pip install cryptography). Exit code 0 if
valid, 1 if not.
"""

import argparse
import base64
import json
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("envelope", help="path to the envelope JSON, or - for stdin")
    ap.add_argument("--key", required=True, help="the portal's Ed25519 public key, base64url")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.envelope == "-" else open(args.envelope, encoding="utf-8").read()
    env = json.loads(raw)
    payload, signature = env["payload"], env["signature"]
    try:
        Ed25519PublicKey.from_public_bytes(unb64(args.key)).verify(unb64(signature), payload.encode("utf-8"))
    except (InvalidSignature, ValueError):
        print("NOT VALID: the signature does not match this payload under this key")
        return 1
    body = json.loads(payload)
    # The payload must be in canonical form, or two different byte strings
    # could claim to be the same record.
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if canonical != payload:
        print("NOT VALID: signature matches, but the payload is not in canonical form")
        return 1
    print(f"VALID: {body.get('type')} {body.get('id')} issued {body.get('issued_at')} by {body.get('issuer')}")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
