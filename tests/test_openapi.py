import json
import sys

from .conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

import export_openapi  # noqa: E402


def test_committed_openapi_document_is_current():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(export_openapi.document(), sort_keys=True)), \
        "docs/openapi.json is stale: run python tools/export_openapi.py"


def test_document_is_json_api_only_with_bearer_auth():
    doc = export_openapi.document()
    assert all(path.startswith("/api/") for path in doc["paths"])
    assert "HTTPBearer" in doc["components"]["securitySchemes"]
