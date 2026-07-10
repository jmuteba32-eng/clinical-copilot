"""Standalone dev utility: list the FHIR Patient resources on a live OpenEMR
instance, printing just enough to pick a real patient ID for manual testing.

This exists because FHIR `Patient.id` on OpenEMR is the internal `uuid`
column, never the legacy integer `pid` (confirmed by reading
`src/Services/FHIR/FhirPatientService.php::parseOpenEMRRecord`, which sets
the resource's `id` from `$dataRecord['uuid']`) -- so there is no way to
guess a valid test ID; it has to come from OpenEMR itself.

Run from the repo root, with `agent/.env` populated:

    python -m agent.list_patients

Deliberately reuses `_parse_patient` from `tools.py` so the fields printed
here are the exact same minimum-necessary-PHI shape (`IntakeRecord`: id,
age, sex) the running agent is willing to expose. This script is a
developer convenience for finding a test ID, not a license to print more
patient data than the agent itself ever would -- no names, no addresses,
no identifiers beyond the FHIR id needed to run a query.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

# Same rationale as main.py: resolve relative to this file, not the CWD,
# so this works whether invoked as `python -m agent.list_patients` from the
# repo root or `python list_patients.py` from inside `agent/`.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agent.openemr_client import OpenEMRClient, OpenEMRClientError  # noqa: E402
from agent.tools import _extract_bundle_resources, _parse_patient  # noqa: E402


async def _main() -> None:
    async with OpenEMRClient() as client:
        try:
            # _count caps the page size; this is a manual dev tool, not a
            # paginating client, so one page is enough to find a test ID.
            bundle = await client.get_fhir("Patient", params={"_count": "50"})
        except OpenEMRClientError as exc:
            print(f"Could not reach OpenEMR: {exc}")
            return

        resources = _extract_bundle_resources(bundle)
        if not resources:
            print("No Patient resources found on this OpenEMR instance.")
            return

        print(f"Found {len(resources)} patient(s) (showing FHIR id, age, sex only):\n")
        for resource in resources:
            fallback_id = resource.get("id") if isinstance(resource.get("id"), str) else "<unknown>"
            record = _parse_patient(resource, fallback_id)
            if record is None:
                print(f"  id={fallback_id}  (too malformed to parse)")
                continue
            print(f"  id={record.source_id}  age={record.age}  sex={record.sex}")

        print("\nUse one of the ids above as `patient_id` in a POST /ask request.")


if __name__ == "__main__":
    asyncio.run(_main())
