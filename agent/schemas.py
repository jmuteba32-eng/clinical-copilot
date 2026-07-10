"""Typed contracts for Clinical Co-Pilot tools.

Every tool the agent can call reads patient data through OpenEMR's FHIR API
and returns one of the models defined here. The contracts exist to make two
guarantees mechanical rather than aspirational:

1. Traceability: every record inherits ``SourcedRecord``, so every claim the
   agent surfaces to a physician can be traced back to the exact FHIR
   resource it came from. A claim with no ``source_id`` cannot be produced by
   a well-typed tool, because the field is required, not optional.
2. Honesty about gaps: OpenEMR charts are frequently incomplete (a lab never
   resulted, a field never populated by the front-office). Fields that can be
   legitimately absent are typed ``Optional`` with no fabricated default. A
   missing value renders as "not recorded", never as a plausible-looking
   zero, empty string, or invented date. See ARCHITECTURE.md ("Missing data
   surfaced, never filled") and USERS.md ("Never fabricate around missing
   data").

These models are pure data contracts: no I/O, no FHIR-parsing logic. Tools
are responsible for mapping raw FHIR JSON into these shapes; keeping that
mapping logic out of this file means the contracts stay stable even if the
FHIR client implementation changes.
"""

from __future__ import annotations

from datetime import date
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field


class SourcedRecord(BaseModel):
    """Base class every record type must inherit from.

    Why this exists: the agent's core trust guarantee is "every claim maps to
    a source ID" (ARCHITECTURE.md, section 4). Putting the source fields on a
    shared base class -- instead of repeating them per model -- makes that
    guarantee structural: a developer cannot add a new record type without
    also giving it a source_id, because the base class requires one. There is
    no way to construct a record that lacks traceability.

    source_date is Optional (unlike source_id/source_type) because OpenEMR
    resources are not guaranteed to carry a date -- e.g. a Condition entered
    without an onset/recorded date is common in practice. Omitting the date
    is a fact about the record, not a bug in the tool; the model must be able
    to represent that honestly.
    """

    source_id: str = Field(
        ...,
        description=(
            "The FHIR resource ID this record was built from (e.g. the "
            "Condition.id). Required on every record so any claim the agent "
            "makes can be pointed back at the exact record a physician "
            "could open in the chart to verify it."
        ),
    )
    source_type: str = Field(
        ...,
        description=(
            'The FHIR resource type this record came from (e.g. "Condition", '
            '"Encounter", "MedicationRequest", "Observation"). Required '
            "alongside source_id because a resource ID alone is ambiguous "
            "without knowing which FHIR endpoint/table it was read from."
        ),
    )
    source_date: Optional[date] = Field(
        default=None,
        description=(
            "When the underlying record was created or clinically recorded. "
            "Optional: OpenEMR does not guarantee every resource carries a "
            "date, and a missing date must render as 'unknown', never as a "
            "guessed or invented one."
        ),
    )


class DiagnosisRecord(SourcedRecord):
    """A single problem-list / condition entry.

    Backs get_diagnosis (ARCHITECTURE.md section 2, first-visit tool). Fields
    mirror FHIR Condition: a coded value (for unambiguous machine matching),
    a human-readable display name (for showing the physician something
    legible without a code lookup), a clinical status, and an onset date.

    clinical_status and onset_date are Optional because problem-list entries
    in OpenEMR are frequently entered without one or both -- a status of
    "unknown" is itself clinically meaningful and must be distinguishable
    from a status that was actually recorded as, say, "resolved".
    """

    code: Optional[str] = Field(
        default=None,
        description=(
            "Coded diagnosis value (e.g. ICD-10/SNOMED). Optional: some "
            "OpenEMR problem entries are free-text only and were never "
            "coded."
        ),
    )
    display: str = Field(
        ...,
        description=(
            "Human-readable condition name. Required (not Optional) because "
            "a diagnosis record with neither a code nor a display name "
            "carries no clinical meaning and should not be constructed by "
            "a tool in the first place -- that case belongs in ToolResult's "
            "missing/error path, not as an empty-looking record."
        ),
    )
    clinical_status: Optional[str] = Field(
        default=None,
        description=(
            'e.g. "active", "resolved", "inactive". Optional because status '
            "is not always populated on legacy OpenEMR problem entries."
        ),
    )
    onset_date: Optional[date] = Field(
        default=None,
        description=(
            "When the condition itself began (distinct from source_date, "
            "which is when the FHIR record was recorded/entered -- the two "
            "can differ by years for a chronic condition documented late)."
        ),
    )


class MedicationRecord(SourcedRecord):
    """A single medication order/statement.

    Backs get_treatment (ARCHITECTURE.md section 2). Mirrors FHIR
    MedicationRequest/MedicationStatement: drug name, dosage, sig
    (patient-facing instructions), a start/end date pair, and a status.

    end_date is Optional for the common and clinically important case of an
    ongoing medication with no end date -- that absence must be
    distinguishable from a medication that has a real end date in the past.
    dosage and sig are both Optional and kept separate because OpenEMR can
    populate one without the other (e.g. structured dosage present but the
    free-text sig field left blank, or vice versa); collapsing them into one
    field would silently drop whichever was present.
    """

    drug_name: str = Field(
        ...,
        description=(
            "Name of the medication. Required for the same reason as "
            "DiagnosisRecord.display: a medication record identifying no "
            "drug is not a usable record."
        ),
    )
    dosage: Optional[str] = Field(
        default=None,
        description=(
            "Structured dose/route/frequency if OpenEMR recorded one. "
            "Optional because free-text-only orders exist."
        ),
    )
    sig: Optional[str] = Field(
        default=None,
        description=(
            "Patient-facing instructions (e.g. 'take one tablet by mouth "
            "twice daily'). Optional and separate from dosage because the "
            "two fields are populated independently in OpenEMR."
        ),
    )
    start_date: Optional[date] = Field(
        default=None,
        description="When the medication was started. Optional: back-filled or migrated orders can lack this.",
    )
    end_date: Optional[date] = Field(
        default=None,
        description=(
            "When the medication was stopped. Optional and, critically, "
            "None means 'still active or unknown' -- never assume a missing "
            "end_date means the medication ended."
        ),
    )
    status: Optional[str] = Field(
        default=None,
        description='e.g. "active", "stopped", "completed". Optional because status is not always coded in legacy data.',
    )


class TestingRecord(SourcedRecord):
    """A single lab or imaging result.

    Backs get_testing (ARCHITECTURE.md section 2). Mirrors FHIR Observation
    (labs) and DiagnosticReport (imaging) loosely enough to cover both without
    needing two near-identical models: a name, a value/unit pair for
    discrete results, and free-text report content for narrative results
    (e.g. a radiology read has no single numeric value).

    value/unit and report_text are all Optional and independently so, because
    a lab result and an imaging report populate different subsets of these
    fields -- a CBC has value+unit but no report_text; a chest X-ray has
    report_text but no value+unit. No single result type populates all of
    them, so none can be required.
    """

    test_name: str = Field(
        ...,
        description=(
            "Name of the test/panel/study (e.g. 'Hemoglobin A1c', 'Chest "
            "X-ray'). Required for the same reason as other record 'names' "
            "above: unnamed results are not usable records."
        ),
    )
    value: Optional[str] = Field(
        default=None,
        description=(
            "Discrete result value, kept as str (not float) because lab "
            "values can be non-numeric (e.g. 'positive', '<0.1', 'trace') "
            "and coercing to float would either crash or silently drop "
            "real, clinically meaningful data."
        ),
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit for value (e.g. 'mg/dL'). Optional and only meaningful when value is a bare number.",
    )
    report_text: Optional[str] = Field(
        default=None,
        description=(
            "Narrative report content (e.g. a radiology read). Optional: "
            "present for imaging/narrative studies, absent for simple "
            "discrete lab values."
        ),
    )
    test_date: Optional[date] = Field(
        default=None,
        description=(
            "When the specimen was collected or the study performed. "
            "Distinct from source_date (when the FHIR record was recorded), "
            "since results are often entered well after collection."
        ),
    )


class IntakeRecord(SourcedRecord):
    """Minimal patient-context demographics for the opening recap.

    Backs get_intake (ARCHITECTURE.md section 2). This model is
    deliberately the smallest of the four: it exists only to answer "who is
    this patient" at the top of a recap (USERS.md UC-1), not to be a general
    Patient-resource mirror. Fields such as name, address, phone, or SSN are
    intentionally absent -- minimum-necessary PHI (per the design principle
    in this project) means a field is only added when a concrete use case
    needs it, and no current use case needs those.

    age and sex are both Optional because OpenEMR patient records can lack a
    recorded birth date or administrative sex; the recap must be able to say
    "age unknown" rather than guess.
    """

    patient_id: str = Field(
        ...,
        description=(
            "FHIR Patient resource ID. Required: every other tool call in a "
            "session is scoped to this patient, so a context record with no "
            "patient identity is not usable."
        ),
    )
    age: Optional[int] = Field(
        default=None,
        description="Patient age in years, derived from birth date. Optional: birth date is not always recorded.",
    )
    sex: Optional[str] = Field(
        default=None,
        description="Administrative sex as recorded in OpenEMR. Optional for the same reason as age.",
    )


T = TypeVar("T", bound=SourcedRecord)


class ToolResult(BaseModel, Generic[T]):
    """Uniform envelope every tool returns, regardless of record type.

    Why a generic wrapper instead of each tool returning a bare list: the
    agent's failure-mode contract (ARCHITECTURE.md section 6 -- "Tool fails
    -> say so, no guess") requires that "no data" and "call failed" be
    represented, not just "here are the records I found". A bare ``list[T]``
    can only ever be empty; it cannot distinguish "confirmed no diagnoses on
    file" from "the FHIR call to fetch diagnoses errored out", and the agent
    would either report both as silence (a fabrication -- the physician sees
    no gap flagged) or crash. This wrapper makes the distinction a field on
    the type, so the agent can branch on it explicitly.

    Generic[T] (rather than one wrapper class per record type) keeps the
    per-tool boilerplate to a single ``ToolResult[DiagnosisRecord]`` type
    alias at the call site, instead of a hand-written DiagnosisToolResult,
    MedicationToolResult, etc. that would drift out of sync as fields change.

    missing and error are intentionally both present and independent, and
    together describe THREE distinct states a physician needs told apart:

    (a) records non-empty: the tool ran fine and found data. The normal,
        happy-path case.
    (b) missing=True, error=None: the tool ran fine; the patient genuinely
        has no records of this type. Reportable to the physician as a real
        gap -- BUT ONLY trustworthy as that fact once something else has
        independently confirmed the patient itself exists. OpenEMR's FHIR
        search endpoints (which every tool in tools.py calls -- see
        get_intake's docstring for why even the "single patient" tool
        searches rather than reads directly) return HTTP 200 with an empty
        Bundle for BOTH "no patient with this id" and "patient exists but
        has zero records of this type". Those are NOT the same clinical
        fact, and a `ToolResult` alone cannot tell them apart: a
        nonexistent patient_id produces `missing=True` from all four tools
        that looks identical to a real, if sparse, chart. `tools.py`'s
        `patient_exists()` is the caller's job to run FIRST (see
        `main.py`'s /ask handler) specifically so that by the time any
        tool's `missing=True` is trusted and reported to a physician as
        "no diagnoses/meds/labs/demographics on file", patient existence is
        already a confirmed fact, not an assumption.
    (c) missing=False, error="...": the tool could not complete (auth
        failure, network error, malformed FHIR response); records is
        necessarily empty, but this is NOT the same fact as "no data
        exists" and must not be phrased that way to the physician.

    missing=True and error both set is intentionally not forbidden by the
    type: a partial failure (e.g. one of several FHIR calls in the tool
    failed) can legitimately report zero records alongside an error
    describing what did not load, so the physician sees a gap AND the
    reason for it.
    """

    records: list[T] = Field(
        default_factory=list,
        description=(
            "Records found, in whatever order the tool produced them. "
            "Defaulted to an empty list (not Optional) because 'no records' "
            "is fully expressed by an empty list plus missing=True; the "
            "field itself should never be absent/null."
        ),
    )
    missing: bool = Field(
        default=False,
        description=(
            "True when the tool ran successfully but found zero records for "
            "this patient. Lets the agent state a gap as a confirmed fact "
            "('no imaging on file') rather than staying silent, per "
            "USERS.md's 'gaps stated' requirement."
        ),
    )
    error: Optional[str] = Field(
        default=None,
        description=(
            "Populated only when the tool itself failed (e.g. auth denied "
            "by the FHIR access-control layer, network/timeout, malformed "
            "response). Optional/None is the success path. This is an "
            "internal diagnostic string, not verified for PHI content -- "
            "callers displaying it to a physician should treat it as "
            "developer-facing detail, not a patient-safe message."
        ),
    )
