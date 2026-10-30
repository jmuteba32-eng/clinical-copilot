"""Diagnostic: raw FHIR Condition bundle vs get_diagnosis parsed output.

Run from repo root with agent/.env populated:

    python -m agent.diagnose_conditions <patient_id>
    python -m agent.diagnose_conditions <patient_id> --find-name "Daniel Williams"

Prints unparsed FHIR JSON and parsed DiagnosisRecord rows side by side so we
can tell whether OpenEMR exposes Conditions at all or our parser drops them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from agent.list_patients import list_openemr_patients  # noqa: E402
from agent.openemr_client import OpenEMRClient, OpenEMRClientError  # noqa: E402
from agent.tools import (  # noqa: E402
    _extract_bundle_resources,
    _parse_condition,
    get_diagnosis,
)

JOHN_DOE = "a250a64d-95b9-4e2b-bdfd-9ab3fc887ad1"


async def _find_patient_by_name(client: OpenEMRClient, name: str) -> list[dict[str, Any]]:
    patients = await list_openemr_patients(client)
    needle = name.lower()
    return [p for p in patients if p.get("name") and needle in str(p["name"]).lower()]


async def _diagnose(client: OpenEMRClient, patient_id: str) -> None:
    print("=" * 72)
    print(f"PATIENT_ID: {patient_id}")
    print("=" * 72)

    # --- Raw FHIR (exactly what OpenEMR returns) ---
    print("\n## RAW FHIR: GET /Condition?patient=<id>\n")
    try:
        bundle = await client.get_fhir("Condition", params={"patient": patient_id})
    except OpenEMRClientError as exc:
        print(f"FHIR request FAILED: {type(exc).__name__}: {exc}")
        return

    print("### Full Bundle JSON")
    print(json.dumps(bundle, indent=2, default=str))

    resources = _extract_bundle_resources(bundle)
    print(f"\n### Bundle summary: total={bundle.get('total')!r} entry_count={len(resources)}")

    for i, resource in enumerate(resources, 1):
        print(f"\n--- Raw Condition resource #{i} ---")
        print(json.dumps(resource, indent=2, default=str))

    # --- Per-resource parse attempt (shows drops) ---
    print("\n## PARSE PASS: _parse_condition per raw resource\n")
    for i, resource in enumerate(resources, 1):
        rid = resource.get("id", "<no id>")
        parsed = _parse_condition(resource)
        if parsed is None:
            print(f"  #{i} id={rid!r} -> DROPPED (parser returned None)")
        else:
            print(
                f"  #{i} id={rid!r} -> OK display={parsed.display!r} "
                f"code={parsed.code!r} status={parsed.clinical_status!r}"
            )

    # --- get_diagnosis aggregate ---
    print("\n## get_diagnosis() ToolResult\n")
    result = await get_diagnosis(client, patient_id)
    print(f"  missing={result.missing}")
    print(f"  error={result.error!r}")
    print(f"  record_count={len(result.records)}")
    for rec in result.records:
        print(
            f"  [{rec.source_type}/{rec.source_id}] "
            f"display={rec.display!r} code={rec.code!r} status={rec.clinical_status!r}"
        )

    # --- Side-by-side verdict ---
    print("\n## COMPARISON\n")
    raw_ids = [r.get("id") for r in resources if isinstance(r.get("id"), str)]
    parsed_ids = [rec.source_id for rec in result.records]
    print(f"  Raw FHIR Condition ids:     {raw_ids or '(none)'}")
    print(f"  get_diagnosis record ids:   {parsed_ids or '(none)'}")
    if not resources:
        print("\n  VERDICT (a): FHIR returns ZERO Condition resources for this patient.")
        print("  Diagnoses entered in OpenEMR UI are not visible via FHIR search — not a parser bug.")
    elif len(resources) > len(result.records):
        dropped = set(raw_ids) - set(parsed_ids)
        print(f"\n  VERDICT (b): FHIR returns {len(resources)} resource(s) but get_diagnosis keeps {len(result.records)}.")
        print(f"  Parser dropped ids: {sorted(dropped)}")
    elif raw_ids != parsed_ids:
        print("\n  VERDICT (b): Id mismatch between raw FHIR and parsed output.")
    else:
        print("\n  VERDICT: FHIR and get_diagnosis agree on count and ids.")
        if not resources:
            print("  (Still (a) if count is zero — problem is OpenEMR not exposing Conditions.)")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Compare raw FHIR Conditions vs get_diagnosis")
    parser.add_argument("patient_id", nargs="?", default=JOHN_DOE, help="FHIR Patient uuid")
    parser.add_argument("--find-name", help="Resolve patient_id by picker name substring")
    args = parser.parse_args()

    async with OpenEMRClient() as client:
        patient_id = args.patient_id
        if args.find_name:
            matches = await _find_patient_by_name(client, args.find_name)
            if not matches:
                print(f"No patient matching name {args.find_name!r}")
                sys.exit(1)
            if len(matches) > 1:
                print("Multiple matches:")
                for p in matches:
                    print(f"  {p['id']}  {p.get('name')}  age={p.get('age')} sex={p.get('sex')}")
                print("Re-run with explicit patient_id.")
                sys.exit(1)
            patient_id = str(matches[0]["id"])
            print(f"Resolved {args.find_name!r} -> {patient_id} ({matches[0].get('name')})")

        await _diagnose(client, patient_id)


if __name__ == "__main__":
    asyncio.run(_main())
