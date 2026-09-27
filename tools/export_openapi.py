#!/usr/bin/env python3
"""Write the OpenAPI document to docs/openapi.json (tests/test_openapi.py checks it is current)."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def document() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(data_dir=Path(tmp), demo=False, fixtures_path=ROOT / "fixtures.json",
                            base_url="", secure_cookies=False, admin_email=None, admin_password=None, webhooks=False)
        return create_app(settings).openapi()


def main() -> int:
    out = ROOT / "docs" / "openapi.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
