"""Canonical JSON: the exact byte form that gets hashed and signed.

Sorted keys, no insignificant whitespace, UTF-8. Anyone re-serializing a
record the same way gets the same bytes, which is what makes the hashes and
signatures checkable outside Plumb.
"""

import json


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
