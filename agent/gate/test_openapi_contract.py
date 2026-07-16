"""GATE / contract -- the committed OpenAPI spec matches the implementation.

The rubric requires the spec be "kept in sync with the implementation" and that
"contract tests must verify the implementation matches the spec." This is that
test: it regenerates the spec from the live app and compares it to the committed
file. Add an endpoint or change a model without re-running dump_openapi, and the
push is blocked.

Guards against: published API documentation silently drifting from what the API
actually does.
"""
from __future__ import annotations

import json

from agent.dump_openapi import SPEC_PATH, generate


def test_committed_openapi_spec_exists() -> None:
    assert SPEC_PATH.is_file(), f"{SPEC_PATH.name} is not committed -- run: python -m agent.dump_openapi"


def test_committed_spec_matches_implementation() -> None:
    """If this fails, the spec is stale. Run: python -m agent.dump_openapi"""
    committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(generate(), sort_keys=True))
    assert committed == live, "committed openapi.json is out of sync -- run: python -m agent.dump_openapi"


def test_spec_is_openapi_3() -> None:
    committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert committed["openapi"].startswith("3."), f"expected OpenAPI 3.x, got {committed['openapi']}"


def test_week2_endpoints_are_documented() -> None:
    """The Week 2 surface must appear in the published spec."""
    committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    paths = committed.get("paths", {})
    for required in ("/respond", "/ask", "/chat", "/health", "/ready", "/login"):
        assert required in paths, f"{required} is missing from the published spec"
