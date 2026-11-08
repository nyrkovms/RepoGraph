"""The generated OpenAPI schema is pinned in tests/fixtures/openapi.json.

Refresh it with `make openapi` (UPDATE_OPENAPI_SNAPSHOT=1) after an intended API change.
"""

import difflib
import json
import os
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "openapi.json"


def _dump(schema: dict) -> str:
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _generate() -> str:
    # app.openapi() caches the schema on the app object; drop the cache so every call regenerates it.
    app.openapi_schema = None
    return _dump(app.openapi())


def test_openapi_schema_is_deterministic():
    assert _generate() == _generate()


def test_openapi_matches_snapshot():
    current = _generate()
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(current, encoding="utf-8")
        return
    expected = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
    if current != expected:
        diff = list(
            difflib.unified_diff(
                expected.splitlines(), current.splitlines(), "openapi.json (snapshot)", "openapi.json (current)", lineterm=""
            )
        )
        raise AssertionError(
            "The API schema changed. Run `make openapi` to refresh backend/tests/fixtures/openapi.json, "
            "and update SPEC-MVP-API.md in the same PR (CLAUDE.md rule 10).\n" + "\n".join(diff[:40])
        )
