"""Standalone dev utility: list the FHIR Patient resources on a live OpenEMR
instance, printing enough to pick a real patient for manual testing.

This exists because FHIR `Patient.id` on OpenEMR is the internal `uuid`
column, never the legacy integer `pid` (confirmed by reading
`src/Services/FHIR/FhirPatientService.php::parseOpenEMRRecord`, which sets
the resource's `id` from `$dataRecord['uuid']`) -- so there is no way to
guess a valid test ID; it has to come from OpenEMR itself.

Run from the repo root, with `agent/.env` populated:

    python -m agent.list_patients

Reuses `_parse_patient` from `tools.py` for age/sex (the same fields
`get_intake` exposes to the agent). Name is parsed separately here for
chart *selection* in the UI picker and this dev script only -- physicians
choose a chart by name, but the agent's clinical answers stay
minimum-necessary (age/sex, not name). See `_parse_picker_patient_name`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Same rationale as main.py: resolve relative to this file, not the CWD,
# so this works whether invoked as `python -m agent.list_patients` from the
# repo root or `python list_patients.py` from inside `agent/`.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agent.openemr_client import OpenEMRClient, OpenEMRClientError  # noqa: E402
from agent.tools import _extract_bundle_resources, _parse_patient  # noqa: E402


def _parse_picker_patient_name(resource: dict[str, Any]) -> str | None:
    """Display name for chart selection only — not passed to the agent.

    Physicians pick a chart by name; that is legitimate identification at
    the UI boundary. ``get_intake`` / ``_parse_patient`` deliberately omit
    name so clinical Q&A stays minimum-necessary (age/sex). Name never flows
    into tool results, system prompts, or agent answers.
    """
    names = resource.get("name")
    if not isinstance(names, list) or not names:
        return None
    primary = names[0]
    if not isinstance(primary, dict):
        return None

    parts: list[str] = []
    given = primary.get("given")
    if isinstance(given, list):
        parts.extend(item.strip() for item in given if isinstance(item, str) and item.strip())
    elif isinstance(given, str) and given.strip():
        parts.append(given.strip())

    family = primary.get("family")
    if isinstance(family, str) and family.strip():
        parts.append(family.strip())

    if not parts:
        return None
    return " ".join(parts)


async def list_openemr_patients(client: OpenEMRClient) -> list[dict[str, str | int | None]]:
    """Fetch Patient resources from OpenEMR for chart selection.

    Returns ``id``, ``name`` (picker-only), ``age``, and ``sex``. Age/sex use
    the same ``_parse_patient`` path as ``get_intake``; name is extracted only
    here for identification in the picker, not for agent clinical output.
    """
    # _count caps the page size; this is a manual dev tool / UI picker, not a
    # paginating client, so one page is enough to find a test ID.
    bundle = await client.get_fhir("Patient", params={"_count": "50"})

    resources = _extract_bundle_resources(bundle)
    patients: list[dict[str, str | int | None]] = []
    for resource in resources:
        fallback_id = resource.get("id") if isinstance(resource.get("id"), str) else "<unknown>"
        record = _parse_patient(resource, fallback_id)
        if record is None:
            continue
        patients.append(
            {
                "id": record.source_id,
                "name": _parse_picker_patient_name(resource),
                "age": record.age,
                "sex": record.sex,
            }
        )
    return patients


async def _main() -> None:
    async with OpenEMRClient() as client:
        try:
            patients = await list_openemr_patients(client)
        except OpenEMRClientError as exc:
            print(f"Could not reach OpenEMR: {exc}")
            return

        if not patients:
            print("No Patient resources found on this OpenEMR instance.")
            return

        print(f"Found {len(patients)} patient(s):\n")
        for patient in patients:
            label = patient["name"] or "(no name on file)"
            print(
                f"  {label}  age={patient['age']}  sex={patient['sex']}  id={patient['id']}"
            )

        print("\nUse one of the ids above as `patient_id` in a POST /ask request.")


if __name__ == "__main__":
    asyncio.run(_main())
