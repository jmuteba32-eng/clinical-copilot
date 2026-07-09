"""The four read-only tools the Clinical Co-Pilot agent can call.

Each tool maps exactly one (or, for `get_testing`, two) FHIR resource types
onto the typed records in `schemas.py`. Tools do three jobs and nothing
else: call `OpenEMRClient.get_fhir`, defensively parse whatever comes back,
and return a `ToolResult`. They never decide what the answer to a
physician's question is -- that is the agent's job -- and they never touch
OpenEMR directly except through `OpenEMRClient` (see that module's docstring
for why the audited REST/FHIR path is non-negotiable).

Two failure-handling rules apply uniformly, and are explained in depth at
the bottom of this file's accompanying explanation, not repeated per tool:

1. `OpenEMRNotFoundError` (confirmed absence) -> `missing=True, error=None`.
2. Any other `OpenEMRClientError` (auth denied, network down, unexpected
   API error) -> `missing=False, error=<reason>`.

A third, tool-local rule handles data quality, per AUDIT.md's finding of
zero foreign-key constraints and inconsistent field population: a single
malformed record is skipped and logged, never allowed to crash the whole
tool call, and never allowed to silently masquerade as "no records" when
the server actually returned something the parser couldn't use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

from agent.openemr_client import OpenEMRClient, OpenEMRClientError, OpenEMRNotFoundError
from agent.schemas import (
    DiagnosisRecord,
    IntakeRecord,
    MedicationRecord,
    SourcedRecord,
    TestingRecord,
    ToolResult,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=SourcedRecord)


# --------------------------------------------------------------------------
# Shared FHIR-parsing helpers
# --------------------------------------------------------------------------
#
# OpenEMR's FHIR responses cannot be trusted to have a consistent shape --
# the audit found zero foreign-key constraints, mixed date/unit formats,
# and coded-vs-free-text drift. Every helper below is written to degrade to
# `None` on anything unexpected rather than raising, so "field absent" and
# "field present but oddly shaped" both resolve the same way: no fabricated
# value, ever.


def _extract_bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the resource dicts out of a FHIR search Bundle.

    Tolerates a missing, non-list, or malformed `entry` array -- a search
    with zero matches is a normal, valid Bundle (`entry` may be absent
    entirely), not a malformed one.
    """
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return []
    resources: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            resource = entry.get("resource")
            if isinstance(resource, dict):
                resources.append(resource)
    return resources


def _parse_bundle_resources(
    resources: list[dict[str, Any]],
    parser: Callable[[dict[str, Any]], T | None],
    resource_type: str,
) -> tuple[list[T], int]:
    """Run `parser` over each resource, skipping (and logging) any that fail.

    Returns (records, skipped_count). `except Exception` here is a
    deliberate, narrow exception to this project's usual "catch specific
    errors" convention: this is a boundary parsing data from a system with
    no referential-integrity guarantees, and the explicit requirement is
    that one bad record must not crash the whole tool call. `KeyboardInterrupt`
    / `SystemExit` are BaseException subclasses in Python and are not
    caught here, so they still propagate.
    """
    records: list[T] = []
    skipped = 0
    for resource in resources:
        if not isinstance(resource, dict):
            skipped += 1
            continue
        try:
            record = parser(resource)
        except Exception:
            skipped += 1
            logger.warning(
                "Skipped malformed %s resource (id=%s) while parsing FHIR bundle",
                resource_type,
                resource.get("id", "<unknown>"),
                exc_info=True,
            )
            continue
        if record is None:
            skipped += 1
            continue
        records.append(record)
    return records, skipped


def _parse_fhir_date(value: object) -> date | None:
    """Parse a full-precision FHIR date/dateTime string, or return None.

    FHIR permits reduced-precision dates ("1990" or "1990-05"), but this
    project's schemas use day-granularity `date` fields. Rounding a
    reduced-precision date to a specific day would fabricate a day the
    source never recorded -- forbidden by this project's design principles
    -- so a reduced-precision (or absent, or wrongly-typed) value is treated
    the same as a missing one: None. `datetime.fromisoformat` already
    rejects reduced-precision strings with a ValueError, which is exactly
    the behavior wanted here.
    """
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        # `datetime.fromisoformat` wants "+00:00", not the "Z" shorthand FHIR uses.
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        return None


def _first_present_date(resource: dict[str, Any], *field_names: str) -> date | None:
    """Try each field name in priority order, then fall back to `meta.lastUpdated`.

    `meta.lastUpdated` is the one date every FHIR resource is supposed to
    carry (it's server-managed, not clinician-entered), so it's a
    reasonable last-resort "when was this record recorded" answer when the
    resource-specific date field(s) are absent.
    """
    for field_name in field_names:
        parsed = _parse_fhir_date(resource.get(field_name))
        if parsed is not None:
            return parsed
    meta = resource.get("meta")
    if isinstance(meta, dict):
        return _parse_fhir_date(meta.get("lastUpdated"))
    return None


def _code_and_display(codeable_concept: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (code, display) from a FHIR CodeableConcept.

    Prefers the first coding entry's code/display; falls back to the
    concept's own free-text `.text` for display. Necessary because OpenEMR
    data can have either populated, both, or neither (AUDIT.md's
    "coded-vs-free-text drift" finding) -- a concept coded only in free text
    is still a real, displayable diagnosis/drug/test name.
    """
    code: str | None = None
    display: str | None = None
    codings = codeable_concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                candidate_code = coding.get("code")
                candidate_display = coding.get("display")
                if code is None and isinstance(candidate_code, str) and candidate_code:
                    code = candidate_code
                if display is None and isinstance(candidate_display, str) and candidate_display:
                    display = candidate_display
                if code is not None and display is not None:
                    break
    if display is None:
        text = codeable_concept.get("text")
        if isinstance(text, str) and text:
            display = text
    return code, display


def _first_coding_code(codeable_concept: dict[str, Any]) -> str | None:
    """Like `_code_and_display`, but for status-style concepts where only a code/text matters."""
    codings = codeable_concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                code = coding.get("code")
                if isinstance(code, str) and code:
                    return code
    text = codeable_concept.get("text")
    if isinstance(text, str) and text:
        return text
    return None


def _calculate_age(birth_date: date, as_of: date) -> int:
    """Whole years between `birth_date` and `as_of`, accounting for the birthday not yet occurring this year."""
    age = as_of.year - birth_date.year
    if (as_of.month, as_of.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


# --------------------------------------------------------------------------
# Per-resource parsers: raw FHIR dict -> typed record, or None if unusable
# --------------------------------------------------------------------------


def _parse_patient(resource: dict[str, Any], requested_patient_id: str) -> IntakeRecord | None:
    resource_id = resource.get("id")
    resource_id = resource_id if isinstance(resource_id, str) and resource_id else requested_patient_id

    birth_date = _parse_fhir_date(resource.get("birthDate"))
    age = _calculate_age(birth_date, date.today()) if birth_date is not None else None

    sex = resource.get("gender")
    sex = sex if isinstance(sex, str) and sex else None

    return IntakeRecord(
        source_id=resource_id,
        source_type="Patient",
        # Patient has no clinician-authored "recorded on" date; meta.lastUpdated
        # (the only date the resource is guaranteed to carry) is the honest answer.
        source_date=_first_present_date(resource),
        patient_id=resource_id,
        age=age,
        sex=sex,
        # Deliberately not read: name, address, telecom, identifier (SSN/MRN
        # blocks). This tool exists to answer "who is this patient" for
        # clinical context only -- minimum-necessary PHI, not a Patient mirror.
    )


def _parse_condition(resource: dict[str, Any]) -> DiagnosisRecord | None:
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        return None  # Cannot build a SourcedRecord without a source_id.

    code_concept = resource.get("code")
    code, display = _code_and_display(code_concept) if isinstance(code_concept, dict) else (None, None)
    if display is None:
        return None  # DiagnosisRecord.display is required; nothing usable to show.

    status_concept = resource.get("clinicalStatus")
    clinical_status = _first_coding_code(status_concept) if isinstance(status_concept, dict) else None

    onset_date = _parse_fhir_date(resource.get("onsetDateTime"))
    if onset_date is None:
        onset_period = resource.get("onsetPeriod")
        if isinstance(onset_period, dict):
            onset_date = _parse_fhir_date(onset_period.get("start"))

    return DiagnosisRecord(
        source_id=resource_id,
        source_type="Condition",
        source_date=_first_present_date(resource, "recordedDate"),
        code=code,
        display=display,
        clinical_status=clinical_status,
        onset_date=onset_date,
    )


def _structured_dosage_summary(dosage_instruction: dict[str, Any]) -> str | None:
    """Build a compact string like '500 mg' from `doseAndRate`, if present."""
    dose_and_rate = dosage_instruction.get("doseAndRate")
    if not isinstance(dose_and_rate, list) or not dose_and_rate:
        return None
    first_dose = dose_and_rate[0]
    if not isinstance(first_dose, dict):
        return None
    dose_quantity = first_dose.get("doseQuantity")
    if not isinstance(dose_quantity, dict):
        return None
    value = dose_quantity.get("value")
    if value is None:
        return None
    unit = dose_quantity.get("unit")
    return f"{value} {unit}" if isinstance(unit, str) and unit else str(value)


def _parse_medication_request(resource: dict[str, Any]) -> MedicationRecord | None:
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        return None

    drug_name: str | None = None
    med_concept = resource.get("medicationCodeableConcept")
    if isinstance(med_concept, dict):
        _, drug_name = _code_and_display(med_concept)
    if drug_name is None:
        med_reference = resource.get("medicationReference")
        if isinstance(med_reference, dict):
            reference_display = med_reference.get("display")
            if isinstance(reference_display, str) and reference_display:
                drug_name = reference_display
    if drug_name is None:
        return None  # MedicationRecord.drug_name is required; no identifiable drug.

    sig: str | None = None
    dosage: str | None = None
    dosage_instructions = resource.get("dosageInstruction")
    if isinstance(dosage_instructions, list) and dosage_instructions:
        first_instruction = dosage_instructions[0]
        if isinstance(first_instruction, dict):
            sig_text = first_instruction.get("text")
            sig = sig_text if isinstance(sig_text, str) and sig_text else None
            dosage = _structured_dosage_summary(first_instruction)

    start_date: date | None = None
    end_date: date | None = None
    dispense_request = resource.get("dispenseRequest")
    if isinstance(dispense_request, dict):
        validity_period = dispense_request.get("validityPeriod")
        if isinstance(validity_period, dict):
            start_date = _parse_fhir_date(validity_period.get("start"))
            end_date = _parse_fhir_date(validity_period.get("end"))
    if start_date is None:
        start_date = _parse_fhir_date(resource.get("authoredOn"))

    status = resource.get("status")
    status = status if isinstance(status, str) and status else None

    return MedicationRecord(
        source_id=resource_id,
        source_type="MedicationRequest",
        source_date=_first_present_date(resource, "authoredOn"),
        drug_name=drug_name,
        dosage=dosage,
        sig=sig,
        start_date=start_date,
        end_date=end_date,
        status=status,
    )


def _observation_value(resource: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (value, unit) from whichever FHIR `value[x]` variant Observation used.

    Kept as strings, never coerced to float: lab values legitimately include
    non-numeric results ("positive", "<0.1", "trace"), and forcing numeric
    coercion would either crash or silently drop real, clinically meaningful
    data -- exactly the fabrication-by-omission this project forbids.
    """
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict) and quantity.get("value") is not None:
        unit = quantity.get("unit")
        return str(quantity["value"]), (unit if isinstance(unit, str) and unit else None)

    value_string = resource.get("valueString")
    if isinstance(value_string, str) and value_string:
        return value_string, None

    value_concept = resource.get("valueCodeableConcept")
    if isinstance(value_concept, dict):
        _, display = _code_and_display(value_concept)
        if display is not None:
            return display, None

    value_boolean = resource.get("valueBoolean")
    if isinstance(value_boolean, bool):
        return ("true" if value_boolean else "false"), None

    return None, None


def _parse_observation(resource: dict[str, Any]) -> TestingRecord | None:
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        return None

    code_concept = resource.get("code")
    _, test_name = _code_and_display(code_concept) if isinstance(code_concept, dict) else (None, None)
    if test_name is None:
        return None  # TestingRecord.test_name is required; nothing to identify the test.

    value, unit = _observation_value(resource)

    return TestingRecord(
        source_id=resource_id,
        source_type="Observation",
        source_date=_first_present_date(resource, "issued", "effectiveDateTime"),
        test_name=test_name,
        value=value,
        unit=unit,
        report_text=None,  # Observations are discrete values, not narrative reports.
        test_date=_first_present_date(resource, "effectiveDateTime", "issued"),
    )


def _parse_diagnostic_report(resource: dict[str, Any]) -> TestingRecord | None:
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        return None

    code_concept = resource.get("code")
    _, test_name = _code_and_display(code_concept) if isinstance(code_concept, dict) else (None, None)
    if test_name is None:
        return None

    # Deliberately reads ONLY the written conclusion (and, as a fallback,
    # the coded conclusion text) -- never `presentedForm`/media attachments,
    # which can be scanned images or PDFs of the study itself. This tool
    # surfaces the radiologist's/interpreting provider's written report
    # text, never an image, and never an interpretation this agent
    # generated on its own.
    report_text = resource.get("conclusion")
    report_text = report_text if isinstance(report_text, str) and report_text else None
    if report_text is None:
        conclusion_codes = resource.get("conclusionCode")
        if isinstance(conclusion_codes, list):
            texts: list[str] = []
            for concept in conclusion_codes:
                if isinstance(concept, dict):
                    _, text = _code_and_display(concept)
                    if text is not None:
                        texts.append(text)
            if texts:
                report_text = "; ".join(texts)

    test_date = _parse_fhir_date(resource.get("effectiveDateTime"))
    if test_date is None:
        effective_period = resource.get("effectivePeriod")
        if isinstance(effective_period, dict):
            test_date = _parse_fhir_date(effective_period.get("start"))

    return TestingRecord(
        source_id=resource_id,
        source_type="DiagnosticReport",
        source_date=_first_present_date(resource, "issued", "effectiveDateTime"),
        test_name=test_name,
        value=None,
        unit=None,
        report_text=report_text,
        test_date=test_date,
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


async def get_intake(client: OpenEMRClient, patient_id: str) -> ToolResult[IntakeRecord]:
    """Minimal demographics (age, sex) for clinical context. FHIR Patient.

    Never returns name, address, phone, or SSN -- see `IntakeRecord` in
    schemas.py for why those fields don't exist on the model at all, not
    just why this tool omits them.
    """
    try:
        resource = await client.get_fhir(f"Patient/{patient_id}")
    except OpenEMRNotFoundError:
        return ToolResult[IntakeRecord](records=[], missing=True, error=None)
    except OpenEMRClientError as exc:
        return ToolResult[IntakeRecord](records=[], missing=False, error=str(exc))

    record = _parse_patient(resource, patient_id)
    if record is None:
        # Only reachable if the single Patient resource itself came back
        # unusably malformed -- there is no "skip and continue" option for a
        # direct single-resource read, so this is an error, not a gap.
        return ToolResult[IntakeRecord](
            records=[],
            missing=False,
            error=f"Patient {patient_id} resource was returned but was too malformed to parse.",
        )
    return ToolResult[IntakeRecord](records=[record], missing=False, error=None)


async def get_diagnosis(client: OpenEMRClient, patient_id: str) -> ToolResult[DiagnosisRecord]:
    """The patient's problem list. FHIR Condition, searched by patient."""
    try:
        bundle = await client.get_fhir("Condition", params={"patient": patient_id})
    except OpenEMRNotFoundError:
        return ToolResult[DiagnosisRecord](records=[], missing=True, error=None)
    except OpenEMRClientError as exc:
        return ToolResult[DiagnosisRecord](records=[], missing=False, error=str(exc))

    resources = _extract_bundle_resources(bundle)
    records, skipped = _parse_bundle_resources(resources, _parse_condition, "Condition")

    if not records and skipped:
        # The server sent data; none of it was usable. This is a data-
        # quality failure, not a confirmed-empty problem list -- reporting
        # missing=True here would tell the physician "no diagnoses on file"
        # when the truth is "diagnoses exist but this tool couldn't read them".
        return ToolResult[DiagnosisRecord](
            records=[],
            missing=False,
            error=f"{skipped} Condition record(s) were present but too malformed to parse.",
        )
    return ToolResult[DiagnosisRecord](records=records, missing=len(records) == 0, error=None)


async def get_treatment(client: OpenEMRClient, patient_id: str) -> ToolResult[MedicationRecord]:
    """The patient's medications. FHIR MedicationRequest, searched by patient."""
    try:
        bundle = await client.get_fhir("MedicationRequest", params={"patient": patient_id})
    except OpenEMRNotFoundError:
        return ToolResult[MedicationRecord](records=[], missing=True, error=None)
    except OpenEMRClientError as exc:
        return ToolResult[MedicationRecord](records=[], missing=False, error=str(exc))

    resources = _extract_bundle_resources(bundle)
    records, skipped = _parse_bundle_resources(resources, _parse_medication_request, "MedicationRequest")

    if not records and skipped:
        return ToolResult[MedicationRecord](
            records=[],
            missing=False,
            error=f"{skipped} MedicationRequest record(s) were present but too malformed to parse.",
        )
    return ToolResult[MedicationRecord](records=records, missing=len(records) == 0, error=None)


async def get_testing(client: OpenEMRClient, patient_id: str) -> ToolResult[TestingRecord]:
    """Labs and imaging reports, merged. FHIR Observation + DiagnosticReport.

    These are two independent FHIR resource types, fetched with two separate
    calls. A failure fetching one must not be conflated with a failure
    fetching the other, and a confirmed absence of one (e.g. no imaging
    reports on file) must never suppress data successfully fetched from the
    other -- so each is handled independently and the results are merged
    only after both attempts finish.

    Imaging results carry only the written report text (`DiagnosticReport
    .conclusion`), never the image itself and never an interpretation this
    tool generates -- see `_parse_diagnostic_report`.
    """
    records: list[TestingRecord] = []
    error_parts: list[str] = []

    sources: tuple[tuple[str, Callable[[dict[str, Any]], TestingRecord | None]], ...] = (
        ("Observation", _parse_observation),
        ("DiagnosticReport", _parse_diagnostic_report),
    )
    for resource_type, parser in sources:
        try:
            bundle = await client.get_fhir(resource_type, params={"patient": patient_id})
        except OpenEMRNotFoundError:
            continue  # Confirmed absence of this resource type; not an error.
        except OpenEMRClientError as exc:
            error_parts.append(f"{resource_type}: {exc}")
            continue

        resources = _extract_bundle_resources(bundle)
        parsed, skipped = _parse_bundle_resources(resources, parser, resource_type)
        if not parsed and skipped:
            error_parts.append(f"{resource_type}: {skipped} record(s) were present but too malformed to parse.")
            continue
        records.extend(parsed)

    if error_parts:
        # Surface the failure regardless of whether the other source
        # succeeded: a physician looking at "labs only, imaging unavailable"
        # must be told it's partial, not silently handed a shorter-than-
        # expected list with no explanation.
        return ToolResult[TestingRecord](records=records, missing=False, error="; ".join(error_parts))
    return ToolResult[TestingRecord](records=records, missing=len(records) == 0, error=None)


# --------------------------------------------------------------------------
# Anthropic tool-use schemas
# --------------------------------------------------------------------------
#
# One dict per tool, in the shape Claude's Messages API expects for the
# `tools` parameter. Every schema's only argument is `patient_id`: these
# tools are intentionally single-purpose FHIR readers, not a general query
# interface, so there is nothing else for the model to control.

_PATIENT_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patient_id": {
            "type": "string",
            "description": "The FHIR Patient resource ID.",
        }
    },
    "required": ["patient_id"],
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_intake",
        "description": (
            "Get minimal patient demographics (age, sex) for clinical "
            "context. Does not return name, address, phone, or SSN. Use "
            "this to establish who the patient is, not as a contact-"
            "information lookup."
        ),
        "input_schema": _PATIENT_ID_SCHEMA,
    },
    {
        "name": "get_diagnosis",
        "description": (
            "Get the patient's problem list / diagnoses. Each result "
            "includes the source Condition ID so the diagnosis can be "
            "traced back to the exact chart entry."
        ),
        "input_schema": _PATIENT_ID_SCHEMA,
    },
    {
        "name": "get_treatment",
        "description": (
            "Get the patient's medications: drug name, dosage, patient-"
            "facing instructions (sig), start/end dates, and status. Each "
            "result includes the source MedicationRequest ID."
        ),
        "input_schema": _PATIENT_ID_SCHEMA,
    },
    {
        "name": "get_testing",
        "description": (
            "Get the patient's lab results and imaging reports. For "
            "imaging, only the written report text (the interpreting "
            "provider's conclusion) is returned -- never the image itself "
            "and never an interpretation generated by anything other than "
            "the report's author. Each result includes its source "
            "resource ID."
        ),
        "input_schema": _PATIENT_ID_SCHEMA,
    },
]

# Maps each TOOL_SPECS[i]["name"] to its callable, so an agent orchestrator
# that receives a `tool_use` block from Claude (name + input) can dispatch
# directly: `await TOOL_FUNCTIONS[name](client, **tool_input)`.
TOOL_FUNCTIONS: dict[str, Callable[[OpenEMRClient, str], Any]] = {
    "get_intake": get_intake,
    "get_diagnosis": get_diagnosis,
    "get_treatment": get_treatment,
    "get_testing": get_testing,
}
