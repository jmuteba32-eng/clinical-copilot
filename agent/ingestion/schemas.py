"""Week 2 document-ingestion schemas (intake form).

SEPARATE from Week 1's agent/schemas.py on purpose: these model facts
EXTRACTED FROM AN UPLOADED DOCUMENT via a vision model, not facts read from
structured OpenEMR/FHIR resources. They inherit Week 1's SourcedRecord so the
"every record carries a source" guarantee is preserved -- but for a document,
the source is the file + page it was read from, not a FHIR resource id.

Scope is deliberately the PRD-required clinical subset -- demographics (minus
SSN and contact details, which have no clinical use here and are PHI we do not
need), chief concern, current medications, allergies, family history. The
uploaded form contains much more (insurance, pharmacy, consent, a 43-item
review-of-systems checklist); extracting only the clinically-relevant subset
is an intentional minimum-necessary-PHI decision, documented in
W2_ARCHITECTURE.md, not an oversight.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from agent.schemas import SourcedRecord


class MedicationEntry(BaseModel):
    """One current medication as written on the intake form."""

    name: str = Field(..., description="Medication name exactly as written on the form.")
    dose: Optional[str] = Field(
        default=None,
        description="Dose/strength as written. Optional: the form may leave it blank.",
    )


class AllergyEntry(BaseModel):
    """One allergy + its reaction. 'None' on the form is represented explicitly
    as a single entry with allergen='None' -- we do NOT silently emit an empty
    list, because 'the patient stated no allergies' and 'we never asked' are
    clinically different and must stay distinguishable downstream."""

    allergen: str = Field(..., description="Allergen as written (may be 'None').")
    reaction: Optional[str] = Field(default=None, description="Reaction as written. Optional.")


class FamilyHistoryEntry(BaseModel):
    """One relative's family-history row from the intake table."""

    relative: str = Field(..., description="Relative, e.g. 'Mother', 'Father', 'Sibling'.")
    condition: Optional[str] = Field(
        default=None,
        description="Condition listed for this relative, or None if the row is blank/negative.",
    )
    living: Optional[bool] = Field(
        default=None,
        description="True if marked Living=Y, False if Living=N, None if unmarked.",
    )
    deceased_age: Optional[int] = Field(
        default=None, description="Age at death if provided; otherwise None."
    )


class IntakeFormExtraction(SourcedRecord):
    """Strict schema the VLM output MUST validate against before it is trusted.

    The schema is the source of truth -- raw model output that does not conform
    is rejected, not coerced. source_id/source_type/source_date come from
    SourcedRecord; source_page is added here because a document citation must
    point to a page, not just a file (this is also the anchor the required
    bounding-box overlay will hang off later).
    """

    source_page: int = Field(
        ...,
        ge=1,
        description="1-based page number of the source document this record was extracted from.",
    )

    # Demographics (minimum-necessary: no SSN, no address, no contact numbers).
    patient_name: str = Field(..., description="Patient full name as written.")
    date_of_birth: Optional[str] = Field(default=None, description="DOB as written (not normalized).")
    sex: Optional[str] = Field(default=None, description="Sex/gender as marked.")
    race_ethnicity: Optional[str] = Field(default=None, description="Ethnicity/Race as written.")
    height: Optional[str] = Field(default=None, description="Height as written.")
    weight: Optional[str] = Field(default=None, description="Weight as written.")

    # Clinical core.
    chief_concern: str = Field(..., description="Primary health concern as written.")
    concern_onset: Optional[str] = Field(default=None, description="When the issue began, as written.")
    concern_symptoms: Optional[str] = Field(default=None, description="Current symptoms as written.")

    medications: list[MedicationEntry] = Field(
        default_factory=list,
        description="Current medications. Empty list only if none were asked/recorded.",
    )
    allergies: list[AllergyEntry] = Field(
        default_factory=list,
        description="Allergies. A single allergen='None' entry means explicitly none.",
    )
    family_history: list[FamilyHistoryEntry] = Field(
        default_factory=list,
        description="Family-history rows.",
    )


class LabResult(BaseModel):
    """One row from a lab report's results table.

    Every PRD-required lab field lives here except collection date and the
    source citation, which are properties of the REPORT (identical for every
    row) and live on LabReportExtraction.
    """

    test_name: str = Field(..., description="Test name exactly as printed, e.g. 'Hemoglobin (Hb)'.")
    value: str = Field(
        ...,
        description=(
            "Result value exactly as printed, kept as a string. NOT parsed to a "
            "number: real reports carry values like '<0.01', '150000', and "
            "'Not Detected', and coercing them to float would either crash or "
            "silently invent precision. Downstream code that needs a number can "
            "parse it and handle failure explicitly."
        ),
    )
    unit: Optional[str] = Field(default=None, description="Unit as printed, e.g. 'g/dL'. Optional: some rows are unitless.")
    reference_range: Optional[str] = Field(
        default=None,
        description="Reference range as printed, e.g. '13.0 - 17.0'. Kept verbatim, not parsed into bounds.",
    )
    abnormal_flag: Optional[Literal["High", "Low", "Borderline"]] = Field(
        default=None,
        description=(
            "The lab's OWN flag on this row, transcribed -- never inferred. None "
            "means the report printed no flag. Deliberately not a boolean: the "
            "source report distinguishes High, Low, and Borderline, and flattening "
            "that to abnormal=True would destroy the direction of the abnormality, "
            "which is the clinically meaningful part. The agent must not compute "
            "this from value vs range either -- the lab's flag is the authority, "
            "and silently disagreeing with it would be a fabricated claim."
        ),
    )
    section: Optional[str] = Field(
        default=None,
        description="Panel heading this row appeared under, e.g. 'DIFFERENTIAL WBC COUNT'. Optional.",
    )


class LabReportExtraction(SourcedRecord):
    """Strict schema a lab-report VLM extraction MUST validate against.

    Second of the two required document types. Same contract as the intake form:
    raw model output is rejected rather than coerced, and every derived fact is
    traceable to source_id + source_page.

    NOTE ON PATIENT IDENTITY: this report's patient is whoever the document says
    it is. It is NOT assumed to be the session's patient. The extractor's job is
    to transcribe the document faithfully; reconciling report identity against
    chart identity is a separate concern and is not done here (see
    W2_ARCHITECTURE.md section 7).
    """

    source_page: int = Field(..., ge=1, description="1-based page of the source document.")

    patient_name: str = Field(..., description="Patient name as printed on the report.")
    patient_age: Optional[str] = Field(default=None, description="Age as printed, e.g. '21 Years'.")
    patient_sex: Optional[str] = Field(default=None, description="Sex as printed.")

    lab_name: Optional[str] = Field(default=None, description="Issuing laboratory name.")
    referring_physician: Optional[str] = Field(default=None, description="'Ref. By' physician as printed.")
    sample_type: Optional[str] = Field(default=None, description="Primary sample type, e.g. 'Blood'.")
    collection_date: Optional[str] = Field(
        default=None,
        description=(
            "PRD-required. Collection date/time as printed ('Collected on'), verbatim "
            "and unnormalized -- a lab value's meaning depends on WHEN it was drawn, "
            "and guessing a format would be inventing provenance."
        ),
    )
    reported_date: Optional[str] = Field(default=None, description="'Reported on' date/time as printed.")

    results: list[LabResult] = Field(
        default_factory=list,
        description="Every result row on the report, in document order.",
    )
    interpretation: Optional[str] = Field(
        default=None,
        description="The lab's own printed interpretation/comment, if any. Transcribed, never authored.",
    )
