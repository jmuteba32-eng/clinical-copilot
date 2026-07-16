"""Dump the live FastAPI app's OpenAPI 3.0 spec to a committed file.

Run from the repo root after changing any endpoint or request/response model:

    python -m agent.dump_openapi

The spec is generated from the code, never hand-written -- a hand-maintained
spec drifts from the implementation the first time someone is in a hurry, and a
spec that lies is worse than no spec. agent/gate/test_openapi_contract.py fails
the build if the committed file and the live app disagree, so drift is caught at
push time rather than by whoever trusted the docs.
"""
from __future__ import annotations

import json
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent / "openapi.json"


def generate() -> dict:
    from agent.main import app

    return app.openapi()


if __name__ == "__main__":
    spec = generate()
    SPEC_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths = len(spec.get("paths", {}))
    print(f"wrote {SPEC_PATH.name}: OpenAPI {spec.get('openapi')}, {paths} paths")
